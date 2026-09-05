import hashlib
import json
import logging
import re
import threading
import uuid
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import apprise

from .models import Listing

log = logging.getLogger(__name__)

SNIPPET = 300
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
EMAIL_SCHEMES = {"mailto", "mailtos"}


def price(listing: Listing) -> str:
    if listing.price is None:
        return listing.price_text or "цена не указана"
    return f"{listing.price:,}".replace(",", " ") + " ₽"


def facts(listing: Listing) -> str:
    return " · ".join(p for p in (price(listing), listing.address, listing.date_text) if p)


def plural(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "новых объявлений"
    return {1: "новое объявление", 2: "новых объявления", 3: "новых объявления",
            4: "новых объявления"}.get(count % 10, "новых объявлений")


def title(listings: list[Listing]) -> str:
    if len(listings) == 1:
        return f"Avito: {listings[0].title}"
    return f"Avito: {len(listings)} {plural(len(listings))}"


def body(listings: list[Listing]) -> str:
    cards = []
    for li in listings:
        card = [f'<a href="{escape(li.url)}"><b>{escape(li.title)}</b></a>',
                f"<div>{escape(facts(li))}</div>"]
        if li.description:
            desc = li.description[:SNIPPET]
            tail = "…" if len(li.description) > SNIPPET else ""
            card.append(f"<div>{escape(desc)}{tail}</div>")
        cards.append("".join(card))
    return "<hr>".join(cards)


class Notifier:
    def __init__(self, targets: list[str], client: apprise.Apprise | None = None,
                 store: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._configured = bool(targets)
        self._templates: list[tuple[str, str, str]] = []
        known: set[str] = set()
        for target in targets:
            scheme = target.split("://", 1)[0].lower()
            key = "target_" + hashlib.sha256(target.encode()).hexdigest()[:20]
            if key not in known:
                self._templates.append((key, scheme, target))
                known.add(key)
        self.apprise = client if client is not None else apprise.Apprise()
        self._owns_client = client is None
        self.store = store
        self.schemes: list[str] = []
        self._active: dict[str, tuple[str, str]] = {}
        self._invalid: list[str] = []
        email_templates = [target for _, scheme, target in self._templates
                           if scheme in EMAIL_SCHEMES]
        self.template = email_templates[0] if email_templates else (targets[0] if targets else "")
        self._recipients = sorted({email for target in email_templates
                                   for email in self._from_url(target)})
        stored = self._stored()
        if stored and email_templates:
            self._recipients = stored
        self._rebuild()
        if not self._configured:
            log.warning("Доставка не настроена — находки останутся только в логе и на странице")

    def _stored(self) -> list[str]:
        if not self.store or not self.store.exists():
            return []
        try:
            saved = json.loads(self.store.read_text())
        except (ValueError, OSError):
            log.warning("Файл рассылки испорчен — беру адреса из NOTIFY")
            return []
        if not isinstance(saved, list):
            return []
        return sorted({email.strip().lower() for email in saved if isinstance(email, str)
                       and EMAIL.fullmatch(email.strip())})

    @property
    def enabled(self) -> bool:
        with self._lock:
            return bool(self._active)

    @property
    def invalid(self) -> list[str]:
        with self._lock:
            return list(self._invalid)

    @property
    def configuration_error(self) -> bool:
        with self._lock:
            return bool(self._invalid)

    def target_keys(self) -> list[str]:
        with self._lock:
            if not self._configured:
                return ["log"]
            return list(self._active)

    def recipients(self) -> list[str]:
        with self._lock:
            return list(self._recipients)

    def set_recipients(self, emails: list[str]) -> list[str]:
        if any(not isinstance(email, str) for email in emails):
            raise ValueError("не похоже на почту")
        clean = sorted({email.strip().lower() for email in emails if email.strip()})
        bad = [email for email in clean if not EMAIL.fullmatch(email)]
        if bad or not clean:
            raise ValueError("не похоже на почту: " + ", ".join(bad or ["пусто"]))
        with self._lock:
            email_keys = {key for key, scheme, _ in self._templates
                          if scheme in EMAIL_SCHEMES}
            if not email_keys:
                raise ValueError("почтовая доставка не настроена в .env")
            previous = self._recipients
            self._recipients = clean
            self._rebuild()
            if not email_keys.issubset(self._active):
                self._recipients = previous
                self._rebuild()
                raise ValueError("новые адреса не приняты почтовой доставкой")
            if self.store:
                tmp = self.store.with_name(f".{self.store.name}.{uuid.uuid4().hex}.tmp")
                try:
                    tmp.write_text(json.dumps(clean), encoding="utf-8")
                    tmp.replace(self.store)
                except OSError:
                    tmp.unlink(missing_ok=True)
                    self._recipients = previous
                    self._rebuild()
                    raise
            return list(self._recipients)

    def _from_url(self, target: str) -> list[str]:
        parts = urlsplit(target)
        candidates = []
        for key, value in parse_qsl(parts.query):
            if key == "to":
                candidates.extend(re.split(r"[,;]", value))
        for segment in parts.path.split("/"):
            candidates.extend(re.split(r"[,;]", unquote(segment)))
        found = {value.strip().lower() for value in candidates
                 if EMAIL.fullmatch(value.strip())}
        return sorted(found)

    def _rebuild(self) -> None:
        if hasattr(self.apprise, "clear"):
            self.apprise.clear()
        elif self._owns_client:
            self.apprise = apprise.Apprise()
        self._active = {}
        self._invalid = []
        self.schemes = []
        for key, scheme, template in self._templates:
            url = self._email_url(template) if scheme in EMAIL_SCHEMES else template
            try:
                accepted = self.apprise.add(url, tag=key)
            except TypeError:
                accepted = self.apprise.add(url)
            if accepted:
                self._active[key] = (scheme, url)
                self.schemes.append(scheme)
            else:
                self._invalid.append(scheme)
                log.error("Адрес доставки %s:// не понят — проверьте NOTIFY в .env", scheme)

    def _email_url(self, template: str) -> str:
        parts = urlsplit(template)
        kept = [(key, value) for key, value in parse_qsl(parts.query) if key != "to"]
        query = urlencode([*kept, ("to", ",".join(self._recipients))])
        return urlunsplit((parts.scheme, parts.netloc, "", query, parts.fragment))

    def send(self, listings: list[Listing]) -> bool:
        if not listings:
            return True
        keys = self.target_keys()
        delivered = [self.send_to(key, listings) for key in keys]
        return bool(delivered) and all(delivered) and not self.configuration_error

    def send_to(self, target_key: str, listings: list[Listing]) -> bool:
        if not listings:
            return True
        if target_key == "log":
            with self._lock:
                if self._configured:
                    log.error("Неизвестный target доставки: log")
                    return False
                for listing in listings:
                    log.info("НОВОЕ: %s | %s | %s", listing.title, facts(listing), listing.url)
                return True
        return self._notify_target(target_key, title(listings), body(listings))

    def send_text(self, message: str) -> bool:
        keys = self.target_keys()
        if keys == ["log"]:
            log.info(message)
            return True
        delivered = [self._notify_target(
            key, "Avito: сообщение сервиса", escape(message)) for key in keys]
        return bool(delivered) and all(delivered) and not self.configuration_error

    def _notify(self, subject: str, html: str) -> bool:
        keys = self.target_keys()
        delivered = [self._notify_target(key, subject, html) for key in keys if key != "log"]
        return bool(delivered) and all(delivered) and not self.configuration_error

    def _notify_target(self, target_key: str, subject: str, html: str) -> bool:
        with self._lock:
            endpoint = self._active.get(target_key)
            if endpoint is None:
                log.error("Неизвестный target доставки: %s", target_key)
                return False
            ok = self.apprise.notify(title=subject, body=html,
                                     body_format=apprise.NotifyFormat.HTML,
                                     tag=target_key, match_always=False)
            if ok:
                log.info("Отправлено через %s: %s", endpoint[0], subject)
            else:
                log.error("Отправка через %s не удалась: %s", endpoint[0], subject)
            return bool(ok)
