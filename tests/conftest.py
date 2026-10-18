from pathlib import Path

import pytest

from avito.config import Config, Mail
from avito.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "search_page.html"


@pytest.fixture
def real_page() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "avito.db")


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(db=tmp_path / "avito.db", every_minutes=60, max_pages=3,
                  mail=Mail(host="smtp.example.com", sender="from@example.com",
                            to=["to@example.com"]))
