#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/agentrouter-autologin}"
SERVICE="agentrouter-autologin"
PYTHON="${PYTHON:-python3}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите от root: sudo ./deploy.sh"
  exit 1
fi

read -r -p "AGENTROUTER_EMAIL: " EMAIL
read -r -s -p "AGENTROUTER_PASSWORD: " PASSWORD
echo
read -r -p "PROXY (необязательно): " PROXY

mkdir -p "${APP_DIR}"
if [[ ! -f "${APP_DIR}/autologin.py" ]]; then
  echo "Скопируйте проект в ${APP_DIR} или задайте APP_DIR с уже подготовленным проектом."
  exit 1
fi

if ! command -v systemctl >/dev/null; then
  echo "Ошибка: нужен systemd."
  exit 1
fi
if ! command -v google-chrome >/dev/null && ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
  echo "Предупреждение: Chrome/Chromium не найден. Установите его перед запуском."
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
