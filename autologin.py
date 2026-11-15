#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автоматический ежедневный логин на agentrouter.org и логирование баланса.

Использует zendriver (асинхронная автоматизация на базе Chrome DevTools Protocol).
Каждый запуск выполняет вход через логин/пароль, затем читает баланс.

Настройка:
    1. cp .env.example .env  и впишите AGENTROUTER_EMAIL / AGENTROUTER_PASSWORD
    2. pip install -r requirements.txt   # требуется установленный Google Chrome/Chromium
    3. python autologin.py             # ежедневный запуск (headful; на Linux без монитора через Xvfb)
"""

import argparse
import asyncio
import json
import logging
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Файловая блокировка против одновременного запуска двух процессов
# (например, при дублировании cron-записей). Используем простой подход
# с lock-файлом через fcntl (Linux) или msvcrt (Windows).
try:
    import fcntl  # Unix
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False
try:
    import msvcrt  # Windows
    _HAS_MSVCRT = True
except ImportError:
    _HAS_MSVCRT = False

from dotenv import load_dotenv

# Windows: рекомендованная политика цикла событий для asyncio + zendriver
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Конфигурация из .env
load_dotenv()

EMAIL = os.getenv("AGENTROUTER_EMAIL", "")
PASSWORD = os.getenv("AGENTROUTER_PASSWORD", "")
# Необязательный прокси для Chrome (http://, https:// или socks5://).
# Можно также передать логин/пароль в URL: http://user:password@host:port
PROXY = os.getenv("PROXY", "").strip()

BASE_URL = os.getenv("AGENTROUTER_BASE_URL", "https://agentrouter.org")
LOGIN_URL = f"{BASE_URL}/login"
DASHBOARD_URL = os.getenv("AGENTROUTER_DASHBOARD_URL", f"{BASE_URL}/console/personal")

# Каталог профиля браузера
SESSION_DIR = "session_profile"
# Файл с последним зафиксированным балансом
BALANCE_FILE = Path("balance.txt")
# Файл-метка времени последнего успешного получения баланса (для cron-запусков)
SUCCESS_FILE = Path("last_success.txt")
# Lock-файл для защиты от одновременного запуска двух процессов (дублирующийся cron).
# Если процесс держит блокировку — второй сразу завершается, не открывая браузер.
LOCK_FILE = Path("autologin.lock")
# Интервал между успешными запусками: случайно от 24 до 25 часов.
MIN_INTERVAL_SEC = 24 * 60 * 60
MAX_INTERVAL_SEC = 25 * 60 * 60
# CSS-селектор баланса
BALANCE_SELECTOR = ".text-lg.font-semibold"
# Логи
LOG_FILE = Path("logs/autologin.log")
# Файл последнего статуса пропуска (перезаписывается, а не копится в логе)
SKIP_STATUS_FILE = Path("logs/skip_status.txt")
# Режим отладки: сохранять HTML/скриншоты страниц и писать подробный лог.
DEBUG = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")
# Ротация логов: максимальный размер файла и число архивных копий.
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(1024 * 1024)))  # 1 МБ
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))
PAGE_DIR = Path("pages")
# Headful: сайту нужен реальный браузер для ежедневного логина.
HEADLESS = False


def setup_logging() -> logging.Logger:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("agentrouter")
    # В DEBUG-режиме пишем всё (включая детали), иначе только INFO и выше.
    logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


log = setup_logging()


def read_last_balance() -> float | None:
    if not BALANCE_FILE.exists():
        return None
    try:
        return float(BALANCE_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def write_balance(value: float) -> None:
    BALANCE_FILE.write_text(f"{value:.2f}", encoding="utf-8")


def read_last_success() -> float | None:
    """Возвращает UNIX-время последнего успешного получения баланса (или None)."""
    if not SUCCESS_FILE.exists():
        return None
    try:
        return float(SUCCESS_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def write_last_success() -> None:
    SUCCESS_FILE.write_text(str(time.time()), encoding="utf-8")


def next_interval_seconds(last_success: float) -> int:
    """Стабильный случайный интервал до следующей проверки: от 24 до 25 часов."""
    # Один и тот же порог сохраняется между cron-запусками.
    return random.Random(int(last_success)).randint(MIN_INTERVAL_SEC, MAX_INTERVAL_SEC)


def write_skip_status(message: str) -> None:
    """Записывает статус пропуска в отдельный файл (перезапись).

    Файл перезаписывается при каждом вызове, поэтому статус всегда актуален
    и не засоряет основной лог повторяющимися строками «Пропуск».
    """
    SKIP_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    SKIP_STATUS_FILE.write_text(f"{ts} | {message}\n", encoding="utf-8")


def parse_balance_from_text(text: str) -> float | None:
    """Эвристический поиск баланса в тексте страницы: ищем числа рядом со знаком $."""
    # Ищем шаблоны вида $25.00, $ 25, balance: 25.00 и т.п.
    matches = re.findall(r"\$\s*([0-9]+(?:[.,][0-9]{1,2})?)", text)
    for m in matches:
        try:
            return float(m.replace(",", "."))
        except ValueError:
            continue
    # Запасной вариант: "balance" рядом с числом
    m = re.search(r"balan[ce]+\D{0,5}([0-9]+(?:[.,][0-9]{1,2})?)", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    return None


async def is_logged_in(tab, timeout: float = 15.0) -> bool:
    """Проверяем, что мы не на странице логина.

    URL-проверки недостаточно: SPA может показать форму логина на любом URL
    (включая /console), если сессия протухла. Поэтому дополнительно ищем
    маркеры формы входа в тексте страницы.

    Опрашиваем страницу до `timeout` секунд, пока не появится явный маркер
    (логина или дашборда). SPA рендерится асинхронно, поэтому на момент
    первого опроса текст может быть пустым.
    """
    try:
        url = tab.url
    except Exception:
        return False
    if "/login" in url or "login" in url.lower():
        return False

    login_markers = [
        "sign in with email",
        "continue with github",
        "continue with linuxdo",
        "no account? sign up",
        "sign up",
    ]
    dashboard_markers = [
        "logout", "sign out", "выйти", "token", "api key",
        "квота", "quota", "余额", "令牌", "设置", "logs",
        "top up", "recharge", "充值", "usage",
    ]

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            body_text = await tab.evaluate("document.body.innerText") or ""
        except Exception:
            body_text = ""
        body_lower = body_text.lower()

        # Явно форма логина
        for marker in login_markers:
            if marker in body_lower:
                return False
        # Явно дашборд
        for marker in dashboard_markers:
            if marker.lower() in body_lower:
                return True
        # Контента ещё нет — ждём
        await asyncio.sleep(0.5)

    # Не удалось определить за таймаут — считаем не залогиненным (безопаснее)
    return False


async def wait_for_url(tab, predicate, timeout: float) -> bool:
    """Ожидаем, пока URL страницы не начнёт удовлетворять predicate (callable).

    zendriver не имеет встроенного wait_for_url, поэтому опрашиваем tab.url.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            if predicate(tab.url):
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def do_login(tab) -> bool:
    """Заполняем форму логина и ждём прохождения капчи/навигации."""
    log.debug("Заполняю форму логина: %s", EMAIL)

    # Страница логина — SPA. Иногда JS-бандл не отдаётся и React не рендерится
    # → белый экран без формы. Перезагружаем, пока не появится контент.
    for attempt in range(5):
        try:
            await tab.wait_for_ready_state("complete", timeout=15)
        except Exception:
            pass
        # Даём SPA время на рендер; пауза растёт с каждой попыткой (5,10,15,...).
        await asyncio.sleep(5 * (attempt + 1))
        body_text = await tab.evaluate("document.body.innerText") or ""
        if body_text.strip():
            break
        log.warning("Страница логина пустая (белый экран), перезагружаю (попытка %d/5)", attempt + 1)
        await tab.get(LOGIN_URL)

    # Если поля уже видны, не ищем кнопку раскрытия формы.
    try:
        email_el = await tab.select(
            'input[type="email"], input[name="email"], input[name="username"]', timeout=2
        )
    except Exception:
        email_el = None

    if email_el is None:
        # На странице входа иногда сначала нужно раскрыть форму входа по email.
        for label in [
            "Sign in with Email or Username",
            "Sign in with Email",
            "Sign in with email",
            "使用 邮箱或用户名 登录",
            "使用邮箱或用户名登录",
        ]:
            try:
                entry = await tab.find(label, best_match=True, timeout=3)
            except Exception:
                entry = None
            if entry is None:
                continue
            try:
                await entry.click()
                log.debug("Нажал '%s' — открылась форма ввода", label)
                break
            except Exception as e:
                log.warning("Не удалось кликнуть '%s': %s", label, e)
                continue

    try:
        if email_el is None:
            email_el = await tab.select(
                'input[type="email"], input[name="email"], input[name="username"]', timeout=10
            )
        if email_el is None:
            raise RuntimeError("поле email не найдено")
        log.debug("Заполняю email")
        await email_el.send_keys(EMAIL)
        pass_el = await tab.select(
            'input[type="password"], input[name="password"]', timeout=5
        )
        if pass_el is None:
            raise RuntimeError("поле password не найдено")
        log.debug("Заполняю пароль")
        await pass_el.send_keys(PASSWORD)
    except Exception as e:
        log.error("Не удалось заполнить поля логина: %s", e)
        return False

    # Кликаем кнопку входа: сначала стандартный submit, затем по тексту
    submitted = False
    try:
        btn = await tab.select('button[type="submit"], input[type="submit"]', timeout=2)
        if btn is not None:
            await btn.click()
            submitted = True
    except Exception:
        pass

    if not submitted:
        # Ищем кнопку отправки по тексту. Исключаем "Sign in with Email..." —
        # это кнопка раскрытия формы, её мы уже нажали выше.
        for label in ["Log In", "Login", "Log in", "Войти", "登录"]:
            try:
                btn = await tab.find(label, best_match=True, timeout=2)
            except Exception:
                btn = None
            if btn is None:
                continue
            # Пропускаем, если это всё ещё кнопка раскрытия формы
            try:
                btn_text = (btn.text or "").strip().lower()
            except Exception:
                btn_text = ""
            if "with email" in btn_text or "or username" in btn_text:
                continue
            try:
                await btn.click()
                submitted = True
                break
            except Exception as e:
                log.warning("Кнопка '%s' не кликабельна: %s", label, e)
                continue

    if not submitted:
        log.error("Кнопка входа не найдена")
        return False

    # Ждём перехода с /login после отправки формы.
    if await wait_for_url(tab, lambda u: "/login" not in u, timeout=30):
        log.info("Вход выполнен")
        return True
    log.error("Вход не выполнен за 30 сек")
    return False


async def open_profile_menu(tab) -> bool:
    """Открываем dropdown профиля в правом верхнем углу.

    Пробуем по очереди: нативный mouse_move (CDP), JS hover, клик.
    React-dropdownи обычно открываются по клику, но некоторые — по hover.
    """
    # Сохраняем скриншот и HTML для отладки
    if DEBUG:
        await tab.save_screenshot(str(PAGE_DIR / "before_profile_menu.png"))
        html = await tab.get_content()
        (PAGE_DIR / "before_profile_menu.html").write_text(html, encoding="utf-8")
        log.debug("Скриншот и HTML перед открытием меню сохранены в %s", PAGE_DIR)

    # Получаем координаты элемента профиля в правом верхнем углу
    coords = await tab.evaluate(
        """(() => {
        const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
        };
        const candidates = [...document.querySelectorAll('button,[role="button"],a,img,svg,div,span')]
            .map((el) => ({ el, r: el.getBoundingClientRect(), tag: el.tagName, cls: el.className.toString().slice(0,60), text: (el.innerText||'').trim().slice(0,40) }))
            .filter(({ el, r }) => visible(el) && r.top >= 0 && r.top < 120 && r.left > window.innerWidth - 400)
            .sort((a, b) => b.r.left - a.r.left || a.r.top - b.r.top);
        if (!candidates.length) return null;
        const c = candidates[0];
        return { x: c.r.left + c.r.width/2, y: c.r.top + c.r.height/2, tag: c.tag, cls: c.cls, text: c.text, count: candidates.length };
        })()"""
    )
    if not coords:
        log.warning("Элемент профиля не найден в правом верхнем углу")
        return False

    log.debug("Найден элемент профиля: tag=%s cls=%s text=%s (x=%.0f, y=%.0f, всего кандидатов=%d)",
             coords.get("tag"), coords.get("cls"), coords.get("text"),
             coords.get("x", 0), coords.get("y", 0), coords.get("count", 0))

    x, y = coords["x"], coords["y"]

    # 1. Нативный mouse_move через CDP (настоящий hover)
    try:
        import zendriver.cdp.input_ as cdp_input
        await tab.send(cdp_input.dispatch_mouse_event("mouseMoved", x=x, y=y))
        await asyncio.sleep(0.5)
        await tab.send(cdp_input.dispatch_mouse_event("mousePressed", x=x, y=y, button=cdp_input.MouseButton.LEFT, click_count=1))
        await tab.send(cdp_input.dispatch_mouse_event("mouseReleased", x=x, y=y, button=cdp_input.MouseButton.LEFT, click_count=1))
        await asyncio.sleep(1)
        log.debug("Нативный hover+click по элементу профиля выполнен")
    except Exception as e:
        log.warning("Нативный hover не удался: %s", e)

    # Проверяем, появился ли Quit в dropdown
    try:
        quit_el = await tab.find("Quit", best_match=True, timeout=2)
        if quit_el:
            log.debug("Dropdown профиля открыт (Quit найден после hover+click)")
            return True
    except Exception:
        pass

    # 2. JS-клик по элементу
    try:
        await tab.evaluate(
            f"document.elementFromPoint({x}, {y})?.click()"
        )
        await asyncio.sleep(1)
        log.debug("JS-клик по элементу профиля выполнен")
    except Exception:
        pass

    # Проверяем снова
    try:
        quit_el = await tab.find("Quit", best_match=True, timeout=2)
        if quit_el:
            log.debug("Dropdown профиля открыт (Quit найден после JS-клик)")
            return True
    except Exception:
        pass

    log.warning("Dropdown профиля не открылся ни одним способом")
    return False


async def do_logout(tab) -> bool:
    """Если сессия активна, выходим через кнопку Quit/Logout в кабинете."""
    log.debug("Проверяю активную сессию перед новым входом")
    await tab.get(DASHBOARD_URL)
    try:
        await tab.wait_for_ready_state("complete", timeout=20)
    except Exception:
        pass
    await asyncio.sleep(3)

    # Если редиректнуло на /login — не залогинены
    try:
        current_url = tab.url
    except Exception:
        current_url = ""
    if "/login" in current_url or "login" in current_url.lower():
        log.debug("Редирект на /login — активной сессии нет, переходим к логину")
        return True

    # Проверяем текст страницы: кнопка "Sign up" означает незалогиненный статус
    try:
        body_text = (await tab.evaluate("document.body.innerText") or "").lower()
    except Exception:
        body_text = ""
    if "sign up" in body_text and "sign in with email" not in body_text and "logout" not in body_text and "sign out" not in body_text:
        log.debug("Найден текст 'Sign up' без маркеров дашборда — активной сессии нет")
        return True

    if not await is_logged_in(tab, timeout=10):
        log.debug("Активной сессии нет, сразу переходим к логину")
        return True

    await open_profile_menu(tab)

    for label in ["Quit", "Logout", "Log out", "Sign out", "Выйти", "退出"]:
        try:
            btn = await tab.find(label, best_match=True, timeout=3)
        except Exception:
            btn = None
        if btn is None:
            continue
        try:
            await btn.click()
            log.debug("Нажал '%s' для выхода из профиля", label)
            break
        except Exception as e:
            log.warning("Не удалось нажать '%s': %s", label, e)
            continue
    else:
        log.error("Активная сессия найдена, но кнопка Quit/Logout не найдена")
        return False

    if await wait_for_url(tab, lambda u: "/login" in u or "login" in u.lower(), timeout=30):
        log.debug("Выход выполнен")
        return True

    # Некоторые SPA после logout остаются на том же URL, проверяем форму логина по маркерам.
    if not await is_logged_in(tab, timeout=10):
        log.debug("Выход выполнен")
        return True

    log.error("После Quit сессия всё ещё активна")
    return False


async def extract_balance(tab, timeout: float = 60.0) -> float | None:
    """Парсим баланс со страницы профиля.

    Страница — SPA: данные баланса подгружаются через API уже после
    того, как документ загружен. Поэтому опрашиваем страницу, пока
    не появится число баланса.

    Селектор .text-lg.font-semibold может возвращать несколько элементов;
    берём тот, где есть '$' и наибольшее число (основной баланс).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if BALANCE_SELECTOR:
            try:
                # Ищем все элементы по селектору, возвращаем все с '$'
                result = await tab.evaluate(
                    """(() => {
                    const els = document.querySelectorAll(%s);
                    const items = [];
                    for (const el of els) {
                        const t = (el.innerText || '').trim();
                        if (t.includes('$')) {
                            items.push(t);
                        }
                    }
                    return JSON.stringify(items);
                    })()""" % json.dumps(BALANCE_SELECTOR)
                )
                texts = json.loads(result) if result else []
                # Берём максимальное значение, пропуская $0.00 (заглушка до загрузки API).
                best = None
                for txt in texts:
                    val = parse_balance_from_text(txt)
                    if val is None:
                        m = re.search(r"([0-9]+(?:[.,][0-9]{1,2})?)", txt)
                        if m:
                            val = float(m.group(1).replace(",", "."))
                    if val is not None and val > 0 and (best is None or val > best):
                        best = val
                if best is not None:
                    return best
            except Exception:
                pass

        # Эвристика: ищем по всему тексту страницы
        try:
            body_text = await tab.evaluate("document.body.innerText")
        except Exception:
            body_text = ""
        val = parse_balance_from_text(body_text or "")
        # Пропускаем $0.00 — это заглушка, реальный баланс подгружается через API позже.
        if val is not None and val > 0:
            return val

        await asyncio.sleep(1)

    log.warning("Баланс не найден за %.0f сек опроса страницы", timeout)
    return None


async def save_page_html(tab, suffix: str = "") -> Path | None:
    """Сохраняет HTML текущей страницы в PAGE_DIR для последующего парсинга баланса.

    Имя файла: dashboard_YYYY-MM-DD_HHMMSS[_suffix].html
    """
    PAGE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    name = f"dashboard_{ts}{('_' + suffix) if suffix else ''}.html"
    path = PAGE_DIR / name
    try:
        html = await tab.get_content()
        path.write_text(html, encoding="utf-8")
        log.debug("HTML страницы сохранён: %s", path)
        return path
    except Exception as e:
        log.warning("Не удалось сохранить HTML: %s", e)
        return None


def parse_html_file(path: Path) -> float | None:
    """Офлайн-парсинг баланса из ранее сохранённого HTML-файла (без браузера)."""
    try:
        html = path.read_text(encoding="utf-8")
    except OSError as e:
        log.error("Не удалось прочитать файл %s: %s", path, e)
        return None

    # Если задан явный селектор — вырезаем нужный фрагмент через regex по id/class
    if BALANCE_SELECTOR:
        # Простейший поиск открывающего тега с этим id/class
        token = BALANCE_SELECTOR.split()[-1]
        m = re.search(re.escape(token) + r".*?>(.*?)</", html, re.DOTALL | re.IGNORECASE)
        chunk = m.group(1) if m else html
    else:
        chunk = html

    val = parse_balance_from_text(chunk)
    return val


async def run(headless: bool) -> int:
    if not EMAIL or not PASSWORD:
        log.error("Не заданы AGENTROUTER_EMAIL / AGENTROUTER_PASSWORD в .env")
        return 2

    # Импорт zendriver (чтобы не падать при отсутствии на этапе --help)
    try:
        import zendriver as zd
    except ImportError:
        log.error("zendriver не установлен. Выполните: pip install -r requirements.txt")
        return 2

    profile_dir = Path(SESSION_DIR).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    log.debug("Профиль браузера: %s", profile_dir)

    # Headful: сайту нужен реальный браузер для ежедневного логина.
    # На Linux-сервере поднимаем виртуальный X-дисплей через Xvfb, если нет
    # настоящего дисплея. SSH X11-форвардинг (DISPLAY=localhost:N) не подходит
    # для автоматизации браузера, поэтому его тоже игнорируем и поднимаем Xvfb.
    effective_headless = headless
    virtual_display = None
    display = os.getenv("DISPLAY", "")
    real_display = bool(display) and not display.startswith(("localhost:", ":localhost"))
    if not effective_headless and sys.platform.startswith("linux") and not real_display:
        try:
            from pyvirtualdisplay import Display
        except ImportError:
            log.error("Для headful-запуска без монитора установите pyvirtualdisplay и xvfb")
            return 2
        virtual_display = Display(visible=False, size=(1920, 1080))
        virtual_display.start()
        log.debug("Виртуальный дисплей Xvfb запущен: %s", os.getenv("DISPLAY"))

    browser_path = next(
        (shutil.which(name) for name in (
            "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"
        ) if shutil.which(name)),
        None,
    )
    if not browser_path:
        log.error("Chrome/Chromium не найден в PATH")
        return 2
    log.debug("Браузер: %s", browser_path)
    config = zd.Config(
        headless=effective_headless,
        user_data_dir=str(profile_dir),
        sandbox=False,  # запуск под root: Chrome не стартует с sandbox
        browser_executable_path=browser_path,
    )
    if PROXY:
        # zendriver передаёт дополнительные аргументы напрямую Chromium.
        # --proxy-server поддерживает HTTP(S) и SOCKS5-прокси.
        config.add_argument(f"--proxy-server={PROXY}")
        log.info("Используется прокси: %s", PROXY.split("@")[-1])
    browser = await zd.start(config)
    try:
        tab = await browser.get(DASHBOARD_URL)

        if not await do_logout(tab):
            log.error("Не удалось выйти из активной сессии")
            return 1

        await tab.get(LOGIN_URL)

        # Каждый запуск должен выполнять вход через логин и пароль.
        log.debug("Выполняю вход (для начисления бонусов)")
        if not await do_login(tab):
            log.error("Вход не выполнен")
            return 1

        # Переходим на страницу профиля, где отображается баланс.
        # Иногда JS-бандл сайта отдаёт ошибку вместо кода, React не рендерится → белый экран.
        # Перезагружаем, пока не появится контент.
        for attempt in range(5):
            await tab.get(DASHBOARD_URL)
            try:
                await tab.wait_for_ready_state("complete", timeout=20)
            except Exception:
                pass
            # Даём SPA время на рендер (JS может подгрузиться с задержкой).
            await asyncio.sleep(3)
            body_text = await tab.evaluate("document.body.innerText") or ""
            if body_text.strip():
                break
            log.warning("Страница пустая (белый экран), перезагружаю (попытка %d/5)", attempt + 1)

        # Страница профиля — SPA: баланс подгружается через API после загрузки документа.
        # extract_balance опрашивает страницу, пока не появится число баланса.
        balance = await extract_balance(tab)

        # HTML дашборда сохраняем только для отладки парсинга.
        if DEBUG:
            await save_page_html(tab)

        if balance is None:
            log.warning("Не удалось распарсить баланс. Для отладки включите DEBUG=true в .env.")
            return 1

        # Сравнение с предыдущим значением
        prev = read_last_balance()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if prev is None:
            log.info("[%s] Баланс: $%.2f (первая запись)", now, balance)
        else:
            delta = balance - prev
            sign = "+" if delta >= 0 else ""
            log.info("[%s] Баланс: $%.2f (предыдущий: $%.2f, изменение: %s$%.2f)", now, balance, prev, sign, delta)
            if delta > 0:
                log.info("Бонус начислен: +$%.2f", delta)

        write_balance(balance)
        write_last_success()
        log.info("Готово. Баланс сохранён.")

        # В headful-режиме даём пользователю увидеть результат (10 сек)
        if not effective_headless:
            log.debug("Закрою браузер через 10 секунд...")
            await asyncio.sleep(10)

        return 0
    finally:
        await browser.stop()
        if virtual_display is not None:
            virtual_display.stop()


def acquire_lock():
    """Пытается получить эксклюзивную блокировку через lock-файл.

    Возвращает открытый файловый объект (держатель блокировки) или None,
    если другой процесс уже работает. Блокировка автоматически снимается
    при закрытии файлового дескриптора (при выходе из процесса).

    Используется fcntl.flock (Linux/macOS) или msvcrt.locking (Windows).
    NB-режим (non-blocking): если блокировка занята — сразу возвращаем None,
    не ожидая освобождения.
    """
    try:
        fd = open(LOCK_FILE, "w")
    except OSError as e:
        log.error("Не удалось открыть lock-файл %s: %s", LOCK_FILE, e)
        return None
    try:
        if _HAS_FCNTL:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif _HAS_MSVCRT:
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        # Блокировка получена — пишем PID для информации
        fd.write(str(os.getpid()))
        fd.flush()
        return fd
    except (OSError, IOError):
        fd.close()
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Автологин agentrouter.org + лог баланса")
    parser.add_argument("--headless", action="store_true", help="Запуск в headless-режиме (обычно не нужен для ежедневного логина)")
    parser.add_argument("--parse-latest", action="store_true", help="Распарсить баланс из последнего сохранённого HTML (без браузера)")
    parser.add_argument("--parse-html", metavar="PATH", help="Распарсить баланс из указанного HTML-файла (без браузера)")
    parser.add_argument("--force", action="store_true", help="Игнорировать интервал 24ч и запустить браузер принудительно")
    args = parser.parse_args()

    # Офлайн-парсинг сохранённых HTML-страниц — блокировка не нужна
    if args.parse_html:
        path = Path(args.parse_html)
        val = parse_html_file(path)
        if val is None:
            log.error("Баланс из %s не извлечён", path)
            return 1
        log.info("Баланс из %s: $%.2f", path, val)
        return 0
    if args.parse_latest:
        files = sorted(PAGE_DIR.glob("dashboard_*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            log.error("В %s нет сохранённых HTML-файлов", PAGE_DIR)
            return 1
        val = parse_html_file(files[0])
        if val is None:
            log.error("Баланс из %s не извлечён", files[0])
            return 1
        log.info("Баланс из %s: $%.2f", files[0], val)
        return 0

    # Защита от одновременного запуска двух процессов (дублирующийся cron).
    # Второй процесс сразу завершается, не открывая браузер и не дублируя логи.
    lock_fd = acquire_lock()
    if lock_fd is None:
        msg = "Пропуск: другой процесс уже выполняет вход."
        write_skip_status(msg)
        print(msg)
        log.debug(msg)
        return 0

    try:
        # Для cron-запусков (каждые ~10 мин): если с прошлого успешного получения
        # баланса прошло меньше MIN_INTERVAL_SEC — ничего не делаем и не поднимаем браузер.
        if not args.force:
            last = read_last_success()
            if last is not None:
                elapsed = time.time() - last
                interval = next_interval_seconds(last)
                if elapsed < interval:
                    remaining = interval - elapsed
                    msg = (
                        "Пропуск: с прошлого успеха прошло %.1f ч (< случайного интервала %.1f ч). "
                        "Следующий запуск через %.1f ч."
                        % (elapsed / 3600, interval / 3600, remaining / 3600)
                    )
                    write_skip_status(msg)
                    print(msg)
                    log.debug(msg)
                    return 0

        headless = args.headless or HEADLESS
        return asyncio.run(run(headless=headless))
    finally:
        lock_fd.close()


if __name__ == "__main__":
    sys.exit(main())
