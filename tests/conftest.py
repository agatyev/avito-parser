from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from avito.config import Settings
from avito.core import FetchResult
from avito.models import Listing
from avito.watch import Watcher

FIXTURE = Path(__file__).parent / "fixtures" / "search_page.html"


class FakeFetch:
    def __init__(self, html: str = "", error: Exception | None = None) -> None:
        self.html = html
        self.error = error
        self.calls = 0

    def __call__(self, cfg, link: str, known_ids=(), detailed: bool = False) -> str | FetchResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return FetchResult(self.html) if detailed else self.html


class FakeNotifier:
    enabled = True

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.schemes = ["fake"]
        self.batches: list[list[Listing]] = []
        self._recipients = ["fake@example.com"]

    def send(self, listings):
        self.batches.append(list(listings))
        return self.ok

    def send_to(self, target_key: str, listings):
        assert target_key == "fake"
        return self.send(listings)

    def send_text(self, message: str) -> bool:
        return self.ok

    def target_keys(self) -> list[str]:
        return ["fake"]

    def recipients(self) -> list[str]:
        return list(self._recipients)

    def set_recipients(self, emails: list[str]) -> list[str]:
        clean = sorted({e.strip().lower() for e in emails if e.strip()})
        if not clean:
            raise ValueError("нужен хотя бы один адрес")
        self._recipients = clean
        return self.recipients()


@pytest.fixture
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db=tmp_path / "avito.db", notify=[], control=[],
                    watch={"max_age_minutes": 525600, "exclude": []},
                    browser={"profile_dir": tmp_path / "profile"})


@pytest.fixture
def watcher(settings, html) -> Watcher:
    watcher = Watcher(settings)
    watcher.fetch = FakeFetch(html)
    watcher.notifier = FakeNotifier()
    yield watcher
    watcher.close()
