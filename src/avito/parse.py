from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

BASE = "https://www.avito.ru"
CARD = '[data-marker="item"]'
TRAVEL_TIME = re.compile(r"(до\s*)?\d+\s*[–-]?\s*\d*\s*(мин|км|м)\.?", re.IGNORECASE)
BLOCKED_MARKERS = (
    "доступ ограничен", "проблема с ip", "geetest", "firewall", "captcha", "капча",
    "подтвердите, что вы не робот", "проверяем, что вы не робот",
    "проверка безопасности", "слишком много запросов", "access denied",
    "verify you are human", "are you a robot", "checking your browser",
)
DECISIVE_MARKERS = (
    "доступ ограничен", "проблема с ip", "подтвердите, что вы не робот",
    "проверяем, что вы не робот", "verify you are human", "access denied",
)
BLOCKED_SELECTORS = (
    'iframe[src*="captcha"]', 'iframe[src*="geetest"]', 'form[action*="captcha"]',
    '[data-marker*="captcha"]', '[data-marker*="challenge"]', '.g-recaptcha', '#captcha',
    '[data-sitekey]',
)
EMPTY_MARKERS = ("ничего не найдено", "к сожалению, мы не нашли")


@dataclass(slots=True)
class Listing:
    id: str
    url: str
    title: str
    price: int | None = None
    price_text: str = ""
    address: str = ""
    date_text: str = ""
    description: str = ""


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
    return ", ".join(part for part in (clean, f"м. {metro}" if metro else "") if part)


def _description(card) -> str:
    meta = card.select_one('[itemprop="description"]')
    if meta is not None and meta.get("content"):
        return " ".join(meta["content"].split())
    longest = max((_text(node) for node in card.find_all("p")), key=len, default="")
    return " ".join(longest.split()) if len(longest) >= 40 else ""


def parse_card(card) -> Listing | None:
    item_id = _item_id(card)
    if not item_id:
        return None
    price, price_text = _price(card)
    return Listing(
        id=item_id,
        url=_url(card, item_id),
        title=_text(card.select_one('[data-marker="item-title"]')),
        price=price,
        price_text=price_text,
        address=_address(card),
        date_text=_text(card.select_one('[data-marker="item-date"]')),
        description=_description(card),
    )


def parse(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    found = []
    seen = set()
    for card in soup.select(CARD):
        listing = parse_card(card)
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
    challenge = any(soup.select_one(selector) is not None for selector in BLOCKED_SELECTORS)
    decisive = any(marker in title or marker in visible for marker in DECISIVE_MARKERS)
    marker = any(marker in title or marker in visible for marker in BLOCKED_MARKERS)
    return challenge or decisive or (marker and soup.select_one(CARD) is None)


def empty_results(html: str) -> bool:
    low = html.lower()
    return 'data-marker="item"' not in low and any(mark in low for mark in EMPTY_MARKERS)
