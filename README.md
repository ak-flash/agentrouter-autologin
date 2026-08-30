# agentrouter-autologin

Автоматический ежедневный логин на [agentrouter.org](https://agentrouter.org) для получения
бонусных $25 в день + логирование баланса (чтобы видеть, начисляется ли бонус).

Используется **zendriver** — асинхронная автоматизация на базе Chrome DevTools Protocol.
Каждый запуск выполняет полный цикл: выход из предыдущей сессии → вход по логину/паролю →
чтение баланса.

## Установка

Требуется Python 3.10+ и установленный Google Chrome / Chromium.

**Windows (PowerShell):**
```powershell
cd e:\My-projects\agentrouter-autologin

# 1. Виртуальное окружение (опционально, но рекомендуется)
python -m venv .venv
.\.venv\Scripts\activate

# 2. Зависимости
pip install -r requirements.txt
```

**Linux (bash):**
```bash
cd /path/to/agentrouter-autologin

# 1. Виртуальное окружение (опционально, но рекомендуется)
python3 -m venv .venv
source .venv/bin/activate

# 2. Зависимости
pip install -r requirements.txt

# 3. Браузер и Xvfb (для headful-запуска без монитора)
sudo apt install -y chromium-browser xvfb
```

## Настройка

Скопируйте `.env.example` в `.env` и впишите свои данные:

```powershell
Copy-Item .env.example .env
```

```ini
AGENTROUTER_EMAIL=you@example.com
AGENTROUTER_PASSWORD=your_password

# Сохранять HTML страниц в pages/ для отладки парсинга баланса
SAVE_DEBUG_PAGES=false

# Минимальный интервал между успешными запусками, в секундах (по умолчанию 86400 = 24ч).
# Используется при частых cron-запусках, чтобы не логиниться чаще раза в сутки.
MIN_INTERVAL_SEC=86400
```

## Использование

### Ежедневный запуск

```powershell
python autologin.py
```

Скрипт:
1. Открывает браузер (headful, на Linux — через Xvfb).
2. Если сессия активна — выходит через меню профиля (Quit).
3. Входит заново по логину/паролю.
4. Переходит на страницу профиля, читает баланс.
5. Записывает баланс в `balance.txt` и лог, а время успеха — в `last_success.txt`.

### Защита от повторных запусков (метка 24ч)

После успешного получения баланса скрипт пишет метку времени в `last_success.txt`.
При следующем запуске, если с прошлого успеха прошло меньше `MIN_INTERVAL_SEC`
(по умолчанию 24 часа), скрипт **ничего не делает и не поднимает браузер** —
пишет строку «Пропуск» в stdout (для cron) и в `logs/skip_status.txt`
(перезаписывается при каждом запуске), после чего завершается.

Это позволяет ставить cron на частый запуск (например, каждые 10 минут): при
белом экране / сбое вход не фиксируется, и следующий запуск повторит попытку;
как только вход удался — сутки браузер больше не поднимается.

Повторяющиеся строки «Пропуск» **не копятся в `autologin.log`**: актуальный статус
пропуска всегда лежит в `logs/skip_status.txt` (одна строка, перезаписывается),
а основной лог остаётся только для реальных событий (вход, баланс, ошибки).

Принудительный запуск в обход интервала:

```powershell
python autologin.py --force
```

### Защита от одновременного запуска (файловая блокировка)

Если в crontab случайно оказалось две записи (например, одна от пользователя,
другая от root), два процесса стартуют одновременно и дублируют каждую строку
лога. Признак — одинаковые сообщения с разницей в миллисекунды, а для Xvfb —
два разных номера дисплея (`:1` и `:3`) в одну секунду.

Чтобы найти дублирующиеся cron-записи на сервере:

```bash
crontab -l          # задачи текущего пользователя
sudo crontab -l     # задачи root
ls /etc/cron.d/     # системные drop-in файлы
```

Начиная с текущей версии скрипт дополнительно защищён файловой блокировкой
(`autologin.lock`): если один процесс уже работает, второй сразу завершается
с сообщением «Пропуск: другой процесс уже выполняет вход.» (в stdout и
`logs/skip_status.txt`, без записи в основной лог) и не открывает
браузер. Блокировка снимается автоматически при выходе процесса.

Пример вывода в `logs/autologin.log`:

```
2026-07-14 09:00:01 | INFO | Баланс: $25.00 (первая запись)
2026-07-15 09:00:02 | INFO | Баланс: $50.00 (предыдущий: $25.00, изменение: +$25.00)
2026-07-15 09:00:02 | INFO | Бонус начислен: +$25.00
```

### Режим headless (опционально)

```powershell
python autologin.py --headless
```

Обычно не нужен — сайт требует реальный браузер для входа.

## Автозапуск

Благодаря метке 24ч (`last_success.txt`) скрипт можно запускать часто — он
сам пропустит запуск, если сегодня вход уже удался. Рекомендуемый вариант —
каждые 10 минут: это повышает шанс поймать рабочую страницу при белых экранах.

### Windows (Планировщик задач)

Создайте задачу через PowerShell (от имени администратора):

```powershell
$action = New-ScheduledTaskAction `
  -Execute "e:\My-projects\agentrouter-autologin\.venv\Scripts\python.exe" `
  -Argument "autologin.py" `
  -WorkingDirectory "e:\My-projects\agentrouter-autologin"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "AgentRouterAutologin" -Action $action -Trigger $trigger -Force
```

Или вручную: Планировщик задач → Создать задачу → Триггер «Ежедневно», повтор
каждые 10 минут → Действие запустить `python.exe` с аргументом `autologin.py`.

### Linux (cron)

Запуск каждые 10 минут (скрипт сам поднимает Xvfb, т.к. в cron нет `DISPLAY`):

```cron
*/10 * * * * cd /home/ubuntu/agentrouter-autologin && /home/ubuntu/agentrouter-autologin/.venv/bin/python autologin.py >> /home/ubuntu/agentrouter-autologin/logs/cron.log 2>&1
```

Важно:
- Укажите **полный путь** к python из venv — cron не активирует venv сам.
- `cd` в папку проекта нужен, чтобы файлы `balance.txt`, `last_success.txt`, `logs/` создавались рядом.
- Для работы headful через Xvfb на сервере должны быть установлены `chromium-browser`/`google-chrome` и `xvfb`.

## Отладка парсинга баланса

Если баланс не находится, включите сохранение HTML в `.env`:

```ini
SAVE_DEBUG_PAGES=true
```

Скрипт сохранит страницу и скриншоты в `pages/`. Офлайн-парсинг сохранённых страниц:

```powershell
# последний сохранённый файл
python autologin.py --parse-latest

# конкретный файл
python autologin.py --parse-html pages\dashboard_2026-07-14_090001.html
```
