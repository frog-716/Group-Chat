from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


@pytest.fixture()
def migrated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    database_path = tmp_path / "test.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("GROUP_INFO_DATABASE_URL", database_url)
    from group_info_system.config import get_settings

    get_settings.cache_clear()
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    return database_url
