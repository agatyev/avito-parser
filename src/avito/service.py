from __future__ import annotations

import logging
import threading
import time

from . import browse
from .config import Config
from .mail import Mailer
from .parse import empty_results, item_ids, parse
from .store import Search, Store

log = logging.getLogger(__name__)

FIRST_RUN_SENDS = 5
RETRY_PAUSE = 15
BLOCKED_NOTICE = ("Avito отдал страницу антибота. Следующий заход через час — обычно этого "
                  "хватает. Если повторяется каждый час, нужен российский резидентный прокси.")


class Service:
    def __init__(self, cfg: Config, store: Store, mailer: Mailer) -> None:
        self.cfg = cfg
        self.store = store
        self.mailer = mailer
        self.profile = cfg.db.parent / "profile"
        self.snapshots = cfg.db.parent / "html"
        self.state = {"running": False, "activity": "", "error": "", "last": "", "next_at": 0.0}
        self.wake = threading.Event()

    def run(self) -> None:
        period = self.cfg.every_minutes * 60
        log.info("Смотрю каждые %d мин, поисков в работе: %d, почта: %s",
                 self.cfg.every_minutes, len(self.store.active()),
                 ", ".join(self.mailer.recipients()) or "не настроена")
        while True:
            started = time.monotonic()
            self.cycle()
            wait = max(30.0, period - (time.monotonic() - started))
            self.state["next_at"] = time.time() + wait
            self.wake.wait(wait)
            self.wake.clear()

    def trigger(self) -> None:
        self.wake.set()

    def cycle(self) -> None:
        searches = self.store.active()
        if not searches:
            return
        self.state.update(running=True, error="")
        try:
            for search in searches:
                try:
                    self.visit(search)
                except browse.Blocked as exc:
                    self.fail(search, str(exc))
                    log.warning(BLOCKED_NOTICE)
                except Exception as exc:
                    self.fail(search, str(exc))
                self.deliver(search)
        finally:
            self.state.update(running=False, activity="")

    def fail(self, search: Search, message: str) -> None:
        self.state["error"] = f"{search.name}: {message}"
        log.error("[%s] заход не удался, повторю через час: %s", search.name, message)

    def visit(self, search: Search) -> None:
        self.state["activity"] = f"{search.name}: захожу на Avito"
        known = self.store.known(search.id)
        html = self.grab(search, known)
        self.snapshot(search.id, html)
        self.state["activity"] = f"{search.name}: разбираю выдачу"
        listings = parse(html)
        if not listings:
            if empty_results(html):
                self.store.visited(search.id)
                log.info("[%s] по запросу ничего не найдено", search.name)
                return
            raise RuntimeError(f"страница без объявлений, снимок в {self.snapshots}")
        cards = len(item_ids(html))
        if cards and len(listings) / cards < 0.9:
            raise RuntimeError(f"разобрано только {len(listings)} из {cards} карточек")
        added = self.store.remember(search.id, listings)
        self.store.visited(search.id)
        if not known:
            self.store.mark_sent(search.id, [item.id for item in listings[FIRST_RUN_SENDS:]])
            log.info("[%s] знакомство: запомнил %d, пришлю %d самых свежих",
                     search.name, len(added), min(FIRST_RUN_SENDS, len(added)))
        else:
            log.info("[%s] разобрано %d, новых %d", search.name, len(listings), len(added))
        self.state["last"] = f"{search.name}: разобрано {len(listings)}, новых {len(added)}"

    def grab(self, search: Search, known: set[str]) -> str:
        for attempt in (1, 2):
            try:
                return browse.fetch(self.profile, search.url, known, self.cfg.max_pages)
            except browse.Blocked:
                raise
            except Exception as exc:
                if attempt == 2:
                    raise
                log.warning("[%s] %s — пробую ещё раз", search.name, exc)
                time.sleep(RETRY_PAUSE)
        raise RuntimeError("заход не удался")

    def deliver(self, search: Search) -> None:
        waiting = self.store.unsent(search.id)
        if not waiting:
            return
        if not self.mailer.ready:
            log.warning("[%s] почта не настроена, объявлений ждёт: %d",
                        search.name, len(waiting))
            return
        self.state["activity"] = f"{search.name}: отправляю {len(waiting)}"
        if self.mailer.send(search.name, waiting):
            self.store.mark_sent(search.id, [item.id for item in waiting])

    def snapshot(self, search_id: str, html: str) -> None:
        self.snapshots.mkdir(parents=True, exist_ok=True)
        (self.snapshots / f"{search_id}.html").write_text(html, encoding="utf-8")
        alive = {search.id for search in self.store.searches()}
        for old in self.snapshots.glob("*.html"):
            if old.stem not in alive:
                old.unlink(missing_ok=True)

    def add(self, name: str, url: str) -> Search:
        checked = browse.check_search(url)
        name = " ".join((name.strip() or browse.title_from_url(url)).split())[:120]
        search = self.store.add(name, checked.geturl())
        log.info("Добавлен поиск: %s", search.name)
        self.trigger()
        return search
