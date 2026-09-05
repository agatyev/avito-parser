from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime


@dataclass(slots=True)
class Listing:
    id: str
    url: str
    title: str
    price: int | None = None
    price_text: str = ""
    address: str = ""
    date_text: str = ""
    published_at: datetime | None = None
    description: str = ""


@dataclass(slots=True)
class Sub:
    id: str
    name: str
    url: str
    enabled: bool = True
    period_minutes: int = 60
    last_at: float = 0.0
    next_at: float = 0.0
    errors: int = 0


@dataclass(slots=True)
class Stats:
    parsed: int = 0
    fresh: int = 0
    sent: int = 0
    errors: int = 0

    def add(self, other: Stats) -> Stats:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))
        return self

    def __str__(self) -> str:
        return (f"разобрано {self.parsed}, новых {self.fresh}, "
                f"отправлено {self.sent}, ошибок {self.errors}")
