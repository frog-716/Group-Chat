from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def ensure_sqlite_parent(database_url: str) -> None:
    prefix = "sqlite:///"
    if database_url.startswith(prefix) and database_url != "sqlite:///:memory:":
        Path(database_url.removeprefix(prefix)).expanduser().parent.mkdir(
            parents=True, exist_ok=True
        )


def create_db_engine(database_url: str) -> Engine:
    ensure_sqlite_parent(database_url)
    engine = create_engine(database_url)
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            if database_url != "sqlite:///:memory:":
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def session_factory(database_url: str) -> sessionmaker[Session]:
    return sessionmaker(create_db_engine(database_url), expire_on_commit=False)


def create_read_only_db_engine(database_url: str) -> Engine:
    """Create a SQLite engine that rejects writes at the database connection."""

    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url == "sqlite:///:memory:":
        raise ValueError("只读运行查询当前仅支持文件型 SQLite 数据库")
    database_path = Path(database_url.removeprefix(prefix)).expanduser().resolve()
    uri = f"file:{quote(str(database_path))}?mode=ro"

    def connect_read_only() -> sqlite3.Connection:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        return connection

    return create_engine("sqlite+pysqlite://", creator=connect_read_only)


def read_only_session_factory(database_url: str) -> sessionmaker[Session]:
    return sessionmaker(
        create_read_only_db_engine(database_url),
        expire_on_commit=False,
        autoflush=False,
    )
