#!/usr/bin/env bash
# Резервная копия базы бота. Безопасно на работающем боте: используется
# горячий бэкап SQLite (VACUUM INTO), а не копирование файла на ходу.
#
#   sudo bash /opt/vpn-bot/deploy/backup.sh
#   crontab: 0 4 * * * bash /opt/vpn-bot/deploy/backup.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/vpn-bot}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
DB="$APP_DIR/data/vpnbot.db"

[[ -f "$DB" ]] || { echo "База не найдена: $DB" >&2; exit 1; }
mkdir -p "$BACKUP_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/vpnbot-$STAMP.db"

"$APP_DIR/.venv/bin/python" - "$DB" "$OUT" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(src)
conn.execute("VACUUM INTO ?", (dst,))
conn.close()
PY

gzip -f "$OUT"
find "$BACKUP_DIR" -name 'vpnbot-*.db.gz' -mtime "+$KEEP_DAYS" -delete
echo "Готово: $OUT.gz"
