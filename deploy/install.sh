#!/usr/bin/env bash
# Установка бота как сервиса systemd на Ubuntu/Debian.
#
#   sudo bash deploy/install.sh
#
# Запускать из распакованной папки проекта. Скрипт идемпотентный:
# повторный запуск обновляет код и перезапускает сервис, не трогая .env и базу.
set -euo pipefail

APP_DIR=/opt/vpn-bot
APP_USER=vpnbot
SERVICE=vpn-bot
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Запустите с правами root: sudo bash deploy/install.sh" >&2
    exit 1
fi

echo "==> Проверяю Python"
if ! command -v python3 >/dev/null; then
    apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip
fi
PY_MINOR="$(python3 -c 'import sys; print(sys.version_info[1])')"
if [[ "$PY_MINOR" -lt 10 ]]; then
    echo "Нужен Python 3.10+, установлен 3.$PY_MINOR" >&2
    exit 1
fi
python3 -c 'import venv' 2>/dev/null || apt-get install -y -qq python3-venv

echo "==> Пользователь $APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin "$APP_USER"

echo "==> Копирую код в $APP_DIR"
mkdir -p "$APP_DIR"
for item in bot scripts requirements.txt deploy .env.example; do
    [[ -e "$SRC/$item" ]] || continue
    rm -rf "${APP_DIR:?}/$item"
    cp -r "$SRC/$item" "$APP_DIR/$item"
done
mkdir -p "$APP_DIR/data"

echo "==> Конфигурация"
if [[ -f "$APP_DIR/.env" ]]; then
    echo "    .env уже есть — оставляю без изменений"
elif [[ -f "$SRC/.env" ]]; then
    cp "$SRC/.env" "$APP_DIR/.env"
    echo "    .env скопирован из проекта"
else
    cp "$SRC/.env.example" "$APP_DIR/.env"
    echo "    создан из шаблона — заполните BOT_TOKEN и ADMIN_IDS!"
fi
chmod 600 "$APP_DIR/.env"

echo "==> Виртуальное окружение и зависимости"
[[ -d "$APP_DIR/.venv" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Сервис systemd"
cp "$SRC/deploy/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

sleep 2
echo
systemctl --no-pager --lines=0 status "$SERVICE" || true
echo
echo "Готово. Логи:      journalctl -u $SERVICE -f"
echo "Правка настроек:   sudo nano $APP_DIR/.env && sudo systemctl restart $SERVICE"
echo "База данных:       sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/scripts/db.py --tables"
