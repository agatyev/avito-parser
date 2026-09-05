from __future__ import annotations

import re
from datetime import datetime

import dateparser
from bs4 import BeautifulSoup

from .models import Listing

BASE = "https://www.avito.ru"
CARD = '[data-marker="item"]'
DATE_SETTINGS = {"PREFER_DATES_FROM": "past", "DATE_ORDER": "DMY"}
TRAVEL_TIME = re.compile(r"(до\s*)?\d+\s*[–-]?\s*\d*\s*(мин|км|м)\.?", re.IGNORECASE)
BLOCKED_MARKERS = (
    "доступ ограничен", "проблема с ip", "geetest", "firewall", "captcha", "капча",
    "подтвердите, что вы не робот", "проверяем, что вы не робот",
    "проверка безопасности", "слишком много запросов", "access denied",
    "verify you are human", "are you a robot", "checking your browser",
)
DECISIVE_BLOCKED_MARKERS = (
    "доступ ограничен", "проблема с ip", "подтвердите, что вы не робот",
    "проверяем, что вы не робот", "verify you are human", "access denied",
)
BLOCKED_SELECTORS = (
    'iframe[src*="captcha"]', 'iframe[src*="geetest"]', 'form[action*="captcha"]',
    '[data-marker*="captcha"]', '[data-marker*="challenge"]', '.g-recaptcha', '#captcha',
    '[data-sitekey]',
)
EMPTY_MARKERS = ("ничего не найдено", "к сожалению, мы не нашли")


def parse_date(text: str, now: datetime | None = None) -> datetime | None:
    if not text:
        return None
    settings = dict(DATE_SETTINGS)
    if now is not None:
        settings["RELATIVE_BASE"] = now
    return dateparser.parse(text.replace("\xa0", " ").strip(" ·•"),
                            languages=["ru"], settings=settings)


def _text(node) -> str:
    return node.get_text(" ", strip=True).replace("\xa0", " ") if node else ""


def _item_id(card) -> str:
    return str(card.get("data-item-id") or (card.get("id") or "").lstrip("i"))


def _url(card, item_id: str) -> str:
    link = card.select_one('[data-marker="item-title"]') or card.select_one('a[itemprop="url"]')
    href = (link.get("href") if link else "") or ""
    if not href and link is not None:
        parent = link.find_parent("a")
        href = parent.get("href", "") if parent else ""
    href = (href or f"/items/{item_id}").split("?")[0]
    return href if href.startswith("http") else BASE + href


def _price(card) -> tuple[int | None, str]:
    meta = card.select_one('[itemprop="price"]')
    raw = (meta.get("content", "") if meta else "").strip()
    text = _text(card.select_one('[data-marker="item-price"]'))
    return (int(raw) if raw.isdigit() else None), text


def _address(card) -> str:
    metro = _text(card.select_one('[data-marker="metro_link"]'))
    node = (card.select_one('[data-marker="item-address"]')
            or card.select_one('[data-marker="item-location"]'))
    raw = _text(node)
    if metro:
        raw = raw.replace(metro, " ")
    raw = TRAVEL_TIME.sub(" ", raw)
    clean = re.sub(r"\s*,\s*", ", ", re.sub(r"\s{2,}", " ", raw)).strip(" ,")
    return ", ".join(p for p in (clean, f"м. {metro}" if metro else "") if p)


def _description(card) -> str:
    meta = card.select_one('[itemprop="description"]')
    if meta is not None and meta.get("content"):
        return " ".join(meta["content"].split())
    longest = max((_text(p) for p in card.find_all("p")), key=len, default="")
    return " ".join(longest.split()) if len(longest) >= 40 else ""


def parse_card(card, now: datetime | None = None) -> Listing | None:
    item_id = _item_id(card)
    if not item_id:
        return None
    price, price_text = _price(card)
    date_text = _text(card.select_one('[data-marker="item-date"]'))
    return Listing(
        id=str(item_id),
        url=_url(card, str(item_id)),
        title=_text(card.select_one('[data-marker="item-title"]')),
        price=price,
        price_text=price_text,
        address=_address(card),
        date_text=date_text,
        published_at=parse_date(date_text, now),
        description=_description(card),
    )


def parse(html: str, now: datetime | None = None) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    found = []
    seen = set()
    for card in soup.select(CARD):
        listing = parse_card(card, now)
        if listing is not None and listing.title and listing.id not in seen:
            seen.add(listing.id)
            found.append(listing)
    return found


def item_ids(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    return list(dict.fromkeys(item_id for card in soup.select(CARD)
                              if (item_id := _item_id(card))))


def blocked(html: str, status: int = 0) -> bool:
    if status in (403, 418, 429):
        return True
    soup = BeautifulSoup(html, "lxml")
    title = _text(soup.title).casefold()
    visible = _text(soup.body or soup).casefold()
    has_cards = soup.select_one(CARD) is not None
    challenge = any(soup.select_one(selector) is not None for selector in BLOCKED_SELECTORS)
    decisive = any(marker in title or marker in visible
                   for marker in DECISIVE_BLOCKED_MARKERS)
    marker = any(marker in title or marker in visible for marker in BLOCKED_MARKERS)
    return challenge or decisive or (marker and not has_cards)


def empty_results(html: str) -> bool:
    low = html.lower()
    return ('data-marker="item"' not in low
            and any(m in low for m in EMPTY_MARKERS))
