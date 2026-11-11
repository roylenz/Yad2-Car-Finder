"""Database engine, migrations and small portable SQL helpers.

`DATABASE_URL` selects the database (any SQLAlchemy URL, e.g. Postgres on Supabase). Without it
the bot uses SQLite at `BOT_DB_PATH`, or `$DATA_DIR/bot.sqlite3` (default `data/bot.sqlite3`).
"""

import datetime
import os
from functools import cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, Table, create_engine, event
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def data_dir() -> Path:
    return Path(os.getenv("DATA_DIR", "data"))


def default_database_url() -> str:
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    path = Path(os.getenv("BOT_DB_PATH") or data_dir() / "bot.sqlite3")
    return sqlite_url(path)


def sqlite_url(path: str | Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


@cache
def get_engine(url: str) -> Engine:
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:
            # WAL lets the dashboard read while the bot writes.
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


_migrated: set[str] = set()


def migrate(url: str) -> None:
    """Upgrade the database to the latest schema (once per process and URL)."""
    if url in _migrated:
        return
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.attributes["engine"] = get_engine(url)
    command.upgrade(config, "head")
    _migrated.add(url)


def utcnow() -> datetime.datetime:
    """Naive UTC timestamp (SQLite has no time zones; all stored times are UTC)."""
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def upsert(
    connection: Connection,
    table: Table,
    rows: list[dict] | dict,
    keys: list[str],
    update: dict | list[str] | None = None,
) -> None:
    """INSERT ... ON CONFLICT for SQLite and Postgres.

    `update` is a list of columns to overwrite from the new row, or a dict of SQL expressions
    built from `excluded` via a callable. None means DO NOTHING.
    """
    insert = postgres_insert if connection.dialect.name == "postgresql" else sqlite_insert
    statement = insert(table).values(rows)
    if update is None:
        statement = statement.on_conflict_do_nothing(index_elements=keys)
    else:
        if isinstance(update, list):
            update = {column: statement.excluded[column] for column in update}
        elif callable(update):
            update = update(statement.excluded)
        statement = statement.on_conflict_do_update(index_elements=keys, set_=update)
    connection.execute(statement)
