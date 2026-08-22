"""Консоль к базе бота.

    python scripts/db.py                      # интерактивный режим
    python scripts/db.py "SELECT * FROM plans"  # разовый запрос
    python scripts/db.py --tables             # список таблиц и число строк

Читает DATABASE_URL из .env, так что путь к файлу подбирать не нужно.
Безопасно запускать при работающем боте: SQLite в режиме WAL допускает
параллельные чтение и запись.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):  # чтобы кириллица не ломалась в консоли Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot.config import settings  # noqa: E402


def db_path() -> Path:
    url = settings.database_url
    if not url.startswith("sqlite"):
        print(f"DATABASE_URL={url}\nЭто не SQLite — подключайтесь через psql или любой GUI.")
        raise SystemExit(1)
    raw = url.split("///", 1)[-1]
    path = Path(raw)
    if not path.is_absolute():
        path = (ROOT / raw).resolve()
    if not path.exists():
        print(f"Файл БД не найден: {path}\nЗапустите бота хотя бы раз — он создаст базу.")
        raise SystemExit(1)
    return path


def render(cursor: sqlite3.Cursor) -> None:
    rows = cursor.fetchall()
    if cursor.description is None:
        print(f"Готово. Затронуто строк: {cursor.rowcount}")
        return
    headers = [d[0] for d in cursor.description]
    if not rows:
        print("(пусто)")
        return

    table = [headers] + [[("" if v is None else str(v)) for v in row] for row in rows]
    widths = [min(max(len(r[i]) for r in table), 40) for i in range(len(headers))]

    def line(values: list) -> str:
        return " | ".join(v[:w].ljust(w) for v, w in zip(values, widths))

    print(line(headers))
    print("-+-".join("-" * w for w in widths))
    for row in table[1:]:
        print(line(row))
    print(f"\nстрок: {len(rows)}")


def show_tables(conn: sqlite3.Connection) -> None:
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for name in names:
        count = conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        print(f"  {name:<16} {count} стр.")


def main() -> None:
    path = db_path()
    conn = sqlite3.connect(str(path), isolation_level=None)
    print(f"БД: {path}\n")

    args = sys.argv[1:]
    if args and args[0] in ("--tables", "-t"):
        show_tables(conn)
        return
    if args:
        render(conn.execute(" ".join(args)))
        return

    print("Таблицы:")
    show_tables(conn)
    print("\nВводите SQL (пустая строка или exit — выход).")
    while True:
        try:
            query = input("sql> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not query or query in ("exit", "quit", "\\q"):
            break
        try:
            render(conn.execute(query))
        except sqlite3.Error as exc:
            print(f"Ошибка: {exc}")
    conn.close()


if __name__ == "__main__":
    main()
