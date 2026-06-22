from collections.abc import Iterator
from pathlib import Path

import pytest

from weight_mcp.config import Settings
from weight_mcp.db import Database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        password="secret",
        public_base_url="https://weight.example.com",
        database_path=tmp_path / "test.sqlite3",
    )


@pytest.fixture
def db(settings: Settings) -> Iterator[Database]:
    database = Database(settings.database_path)
    yield database
    database.close()
