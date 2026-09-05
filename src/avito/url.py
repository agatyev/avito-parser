from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

HOST = "avito.ru"
BY_DATE = "104"
ITEM_SLUG = re.compile(r"_\d{8,}$")
NON_SEARCH_ROOTS = {"items", "user", "profile", "brands"}

WORDS = {
    "kvartiry": "квартиры", "komnaty": "комнаты", "doma_dachi_kottedzhi": "дома",
    "zemelnye_uchastki": "участки", "garazhi_i_mashinomesta": "гаражи",
    "kommercheskaya_nedvizhimost": "коммерческая недвижимость",
    "nedvizhimost": "недвижимость", "avtomobili": "автомобили",
    "predlozheniya_uslug": "услуги", "telefony": "телефоны", "noutbuki": "ноутбуки",
    "mebel_i_interer": "мебель", "velosipedy": "велосипеды",
    "prodam": "продажа", "sdam": "аренда", "kuplyu": "куплю", "snimu": "сниму",
}


def check(url: str):
    parts = urlparse((url or "").strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or (host != HOST and not host.endswith("." + HOST)):
        raise ValueError("Это не ссылка на Avito")
    if parts.username or parts.password:
        raise ValueError("В ссылке не должно быть логина и пароля — "
                         "скопируйте адрес из строки браузера заново")
    if any(s.startswith("on-map") for s in parts.path.split("/")):
        raise ValueError("Это ссылка на карту — с неё объявления не читаются. "
                         "Переключите вид на «Списком» и скопируйте адрес заново")
    return parts


def check_search(url: str):
    parts = check(url)
    segments = [segment.casefold() for segment in parts.path.split("/") if segment]
    root = segments[0] if segments else ""
    last = segments[-1] if segments else ""
    if root in NON_SEARCH_ROOTS or "profile" in segments or ITEM_SLUG.search(last):
        raise ValueError("Это не ссылка на поиск Avito — скопируйте адрес страницы с выдачей")
    return parts


def by_date(url: str) -> str:
    parts = check(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in ("p", "s")]
    return urlunparse(parts._replace(query=urlencode([*query, ("s", BY_DATE)])))


def page(url: str, number: int) -> str:
    if number < 1:
        raise ValueError("Номер страницы должен быть положительным")
    parts = urlparse(by_date(url))
    query = parse_qsl(parts.query, keep_blank_values=True)
    if number > 1:
        query.append(("p", str(number)))
    return urlunparse(parts._replace(query=urlencode(query)))


def _parts(url: str) -> tuple[list[str], str]:
    parts = check(url)
    segments = [s for s in parts.path.split("/") if s][:3]
    slugs = [s.split("-")[0] if i == 2 else s for i, s in enumerate(segments)]
    return slugs, dict(parse_qsl(parts.query)).get("q", "").strip()


def name(url: str) -> str:
    slugs, query = _parts(url)
    bits = [WORDS.get(s, s.replace("_", " ")) for s in slugs]
    if query:
        bits.append(f"«{query}»")
    return ", ".join(b for b in bits if b) or url


def phrase(url: str) -> str:
    slugs, query = _parts(url)
    if query:
        return query
    return next((WORDS[s] for s in slugs if s in WORDS), "")
