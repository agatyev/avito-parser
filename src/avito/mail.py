from __future__ import annotations

import json
import logging
import re
import smtplib
import uuid
from collections.abc import Sequence
from email.message import EmailMessage
from html import escape
from pathlib import Path

from .config import Mail
from .parse import Listing

log = logging.getLogger(__name__)

ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
SNIPPET = 300
TIMEOUT = 60


def price(listing: Listing) -> str:
    if listing.price is None:
        return listing.price_text or "цена не указана"
    return f"{listing.price:,}".replace(",", " ") + " ₽"


def facts(listing: Listing) -> str:
    return " · ".join(part for part in
                      (price(listing), listing.address, listing.date_text) if part)


def plural(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "новых объявлений"
    return {1: "новое объявление", 2: "новых объявления", 3: "новых объявления",
            4: "новых объявления"}.get(count % 10, "новых объявлений")


def subject(name: str, listings: Sequence[Listing]) -> str:
    if len(listings) == 1:
        return f"Avito: {listings[0].title}"
    return f"Avito, {name}: {len(listings)} {plural(len(listings))}"


def as_text(listings: Sequence[Listing]) -> str:
    blocks = []
    for listing in listings:
        lines = [listing.title, facts(listing), listing.url]
        if listing.description:
            lines.insert(2, listing.description[:SNIPPET])
        blocks.append("\n".join(line for line in lines if line))
    return "\n\n".join(blocks)


def as_html(listings: Sequence[Listing]) -> str:
    cards = []
    for listing in listings:
        card = [f'<a href="{escape(listing.url)}"><b>{escape(listing.title)}</b></a>',
                f"<div>{escape(facts(listing))}</div>"]
        if listing.description:
            tail = "…" if len(listing.description) > SNIPPET else ""
            card.append(f"<div>{escape(listing.description[:SNIPPET])}{tail}</div>")
        cards.append("".join(card))
    return "<hr>".join(cards)


class Mailer:
    def __init__(self, cfg: Mail, store: Path | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self._to = self._stored() or list(cfg.to)
        if not cfg.host:
            log.warning("Почта не настроена — объявления останутся только на странице")

    @property
    def ready(self) -> bool:
        return bool(self.cfg.host and self.cfg.sender and self._to)

    def recipients(self) -> list[str]:
        return list(self._to)

    def _stored(self) -> list[str]:
        if not self.store or not self.store.exists():
            return []
        try:
            saved = json.loads(self.store.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            log.warning("Файл рассылки испорчен — беру адреса из MAIL_TO")
            return []
        if not isinstance(saved, list):
            return []
        return sorted({value.strip().lower() for value in saved
                       if isinstance(value, str) and ADDRESS.fullmatch(value.strip())})

    def set_recipients(self, addresses: Sequence[str]) -> list[str]:
        clean = sorted({str(value).strip().lower() for value in addresses if str(value).strip()})
        bad = [value for value in clean if not ADDRESS.fullmatch(value)]
        if bad or not clean:
            raise ValueError("не похоже на почту: " + ", ".join(bad or ["пусто"]))
        if self.store:
            tmp = self.store.with_name(f".{self.store.name}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_text(json.dumps(clean), encoding="utf-8")
                tmp.replace(self.store)
            except OSError:
                tmp.unlink(missing_ok=True)
                raise
        self._to = clean
        return list(self._to)

    def send(self, name: str, listings: Sequence[Listing]) -> bool:
        if not listings:
            return True
        if not self.ready:
            for listing in listings:
                log.info("НОВОЕ [%s]: %s | %s | %s",
                         name, listing.title, facts(listing), listing.url)
            return False
        message = EmailMessage()
        message["Subject"] = subject(name, listings)
        message["From"] = self.cfg.sender
        message["To"] = ", ".join(self._to)
        message.set_content(as_text(listings))
        message.add_alternative(as_html(listings), subtype="html")
        try:
            self._deliver(message)
        except Exception as exc:
            log.error("Письмо не ушло (%s), объявления остаются в очереди: %s",
                      len(listings), exc)
            return False
        log.info("Отправлено письмо на %s: %s", ", ".join(self._to), message["Subject"])
        return True

    def _deliver(self, message: EmailMessage) -> None:
        if self.cfg.port == 465:
            with smtplib.SMTP_SSL(self.cfg.host, self.cfg.port, timeout=TIMEOUT) as smtp:
                self._login(smtp)
                smtp.send_message(message)
            return
        with smtplib.SMTP(self.cfg.host, self.cfg.port, timeout=TIMEOUT) as smtp:
            smtp.starttls()
            self._login(smtp)
            smtp.send_message(message)

    def _login(self, smtp: smtplib.SMTP) -> None:
        if self.cfg.user and self.cfg.password:
            smtp.login(self.cfg.user, self.cfg.password)
