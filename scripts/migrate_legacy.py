"""Убирает из базы остатки реферальной программы и промокодов.

Зачем: столбцы `balance` и `referral_earned` создавались как NOT NULL, а новый код
их больше не заполняет — из-за этого перестают создаваться новые пользователи:

    IntegrityError: NOT NULL constraint failed: users.balance

Старые записи при этом читаются нормально, поэтому проблема видна только на новых.

    python scripts/migrate_legacy.py          # показать, что будет сделано
    python scripts/migrate_legacy.py --apply  # выполнить

Скрипт идемпотентный: повторный запуск ничего не ломает. Перед изменениями делает
резервную копию базы.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot.config import settings  # noqa: E402

LEGACY_COLUMNS = {
    "users": ["balance", "referral_earned", "referrer_id"],
    "payments": ["promo_code"],
}
LEGACY_TABLES = ["promo_uses", "promo_codes"]
MIN_SQLITE = (3, 35)  # с этой версии есть ALTER TABLE ... DROP COLUMN


def db_path() -> Path:
    url = settings.database_url
    if not url.startswith("sqlite"):
        print(f"DATABASE_URL={url}\nМиграция написана для SQLite. Для Postgres:")
        print("  ALTER TABLE users DROP COLUMN balance, DROP COLUMN referral_earned;")
        raise SystemExit(1)
    raw = url.split("///", 1)[-1]
    path = Path(raw)
    if not path.is_absolute():
        path = (ROOT / raw).resolve()
    if not path.exists():
        print(f"База не найдена: {path}")
        raise SystemExit(1)
    return path


def columns(con: sqlite3.Connection, table: str) -> list:
    try:
        return [row[1] for row in con.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def tables(con: sqlite3.Connection) -> list:
    return [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]


def main() -> None:
    apply = "--apply" in sys.argv
    path = db_path()
    con = sqlite3.connect(str(path))
    print(f"База: {path}\nSQLite: {sqlite3.sqlite_version}\n")

    existing_tables = tables(con)
    plan_columns = [
        (table, column)
        for table, cols in LEGACY_COLUMNS.items()
        if table in existing_tables
        for column in cols
        if column in columns(con, table)
    ]
    plan_tables = [t for t in LEGACY_TABLES if t in existing_tables]

    if not plan_columns and not plan_tables:
        print("Чисто: лишних столбцов и таблиц нет, миграция не нужна.")
        return

    print("Будет удалено:")
    for table, column in plan_columns:
        print(f"  столбец {table}.{column}")
    for table in plan_tables:
        print(f"  таблица {table}")

    if not apply:
        print("\nЭто предпросмотр. Чтобы выполнить, запустите с --apply")
        return

    version = tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2])
    if version < MIN_SQLITE and plan_columns:
        print(f"\nНужен SQLite {MIN_SQLITE[0]}.{MIN_SQLITE[1]}+ для удаления столбцов.")
        raise SystemExit(1)

    backup = path.with_name(f"{path.stem}-before-migrate-{datetime.now():%Y%m%d-%H%M%S}.db")
    con.execute("VACUUM INTO ?", (str(backup),))
    print(f"\nРезервная копия: {backup}")

    skipped = []
    for table, column in plan_columns:
        try:
            con.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            print(f"  удалён столбец {table}.{column}")
        except sqlite3.OperationalError as exc:
            # SQLite не отдаёт столбцы, на которые ссылается внешний ключ или индекс.
            # Такие оставляем: они nullable и вставке новых строк не мешают.
            skipped.append((f"{table}.{column}", str(exc)))
            print(f"  пропущен {table}.{column} — {exc}")

    for table in plan_tables:
        try:
            con.execute(f"DROP TABLE {table}")
            print(f"  удалена таблица {table}")
        except sqlite3.OperationalError as exc:
            skipped.append((table, str(exc)))
            print(f"  пропущена таблица {table} — {exc}")

    con.commit()
    con.execute("VACUUM")
    con.close()

    if skipped:
        print(
            "\nЧасть объектов осталась в базе — это не мешает работе: они допускают NULL "
            "и новый код их не заполняет."
        )
    print("\nГотово. Перезапустите бота: sudo systemctl restart vpn-bot")


if __name__ == "__main__":
    main()
