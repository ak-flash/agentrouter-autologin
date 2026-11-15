#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-${SCRIPT_DIR}}"
SERVICE="agentrouter-autologin"
PYTHON="${PYTHON:-python3}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите от root: sudo ./deploy.sh"
  exit 1
fi

# Обновляем код перед деплоем, если проект клонирован из Git.
if [[ -d "${SCRIPT_DIR}/.git" ]]; then
  echo "Обновление кода из Git..."
  git -C "${SCRIPT_DIR}" pull --ff-only
fi

# Используем уже заполненный .env и спрашиваем только отсутствующие значения.
ENV_FILE="${APP_DIR}/.env"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

EMAIL="${AGENTROUTER_EMAIL:-}"
PASSWORD="${AGENTROUTER_PASSWORD:-}"
PROXY="${PROXY:-}"

if [[ -z "${EMAIL}" ]]; then
  read -r -p "AGENTROUTER_EMAIL: " EMAIL
fi
if [[ -z "${PASSWORD}" ]]; then
  read -r -s -p "AGENTROUTER_PASSWORD: " PASSWORD
  echo
fi
if [[ -z "${PROXY}" && ! -f "${ENV_FILE}" ]]; then
  read -r -p "PROXY (необязательно): " PROXY
fi

mkdir -p "${APP_DIR}"
if [[ ! -f "${APP_DIR}/autologin.py" ]]; then
  echo "В ${APP_DIR} не найден autologin.py. Запустите deploy.sh из каталога проекта."
  exit 1
fi

if ! command -v systemctl >/dev/null; then
  echo "Ошибка: нужен systemd."
  exit 1
fi
if ! command -v google-chrome >/dev/null && ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
  echo "Chrome/Chromium не найден. Устанавливаю Chromium..."
  if command -v apt-get >/dev/null; then
    apt-get update
    # В Ubuntu chromium часто является snap-переходником. В окружениях без
    # loop/squashfs snap не работает, поэтому используем Google Chrome .deb.
    if ! DEBIAN_FRONTEND=noninteractive apt-get install -y chromium; then
      echo "Chromium через apt недоступен, устанавливаю Google Chrome..."
      # Удаляем snap-переходник, оставшийся в незавершённом состоянии dpkg.
      dpkg --remove --force-remove-reinstreq chromium-browser 2>/dev/null || true
      apt-get install -y wget ca-certificates
      tmp_deb="$(mktemp --suffix=.deb)"
      trap 'rm -f "${tmp_deb}"' EXIT
      wget -q -O "${tmp_deb}" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
      DEBIAN_FRONTEND=noninteractive apt-get install -y "${tmp_deb}"
      rm -f "${tmp_deb}"
      trap - EXIT
    fi
  elif command -v dnf >/dev/null; then
    dnf install -y chromium
  elif command -v yum >/dev/null; then
    yum install -y chromium
  elif command -v pacman >/dev/null; then
    pacman -Sy --noconfirm chromium
  else
    echo "Ошибка: не найден пакетный менеджер (apt-get/dnf/yum/pacman)."
    exit 1
  fi
fi

if command -v apt-get >/dev/null; then
  echo "Устанавливаю системные зависимости для headful Chromium..."
  apt-get install -y xvfb
fi

if [[ ! -d "${APP_DIR}/.venv" ]]; then
  ${PYTHON} -m venv "${APP_DIR}/.venv"
fi

"${APP_DIR}/.venv/bin/pip" install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

umask 077
cat > "${APP_DIR}/.env" <<EOF
AGENTROUTER_EMAIL=${EMAIL}
AGENTROUTER_PASSWORD=${PASSWORD}
PROXY=${PROXY}
EOF

cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=AgentRouter daily autologin
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=/bin/bash -c 'while true; do ${APP_DIR}/.venv/bin/python autologin.py --headless; sleep 600; done'
Restart=always
RestartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

chmod 600 "${APP_DIR}/.env"
systemctl daemon-reload
systemctl enable --now "${SERVICE}.service"

echo
echo "Готово. Статус: systemctl status ${SERVICE}"
echo "Логи: journalctl -u ${SERVICE} -f"
