from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from .config import Settings
from .core import Blocked, Cancelled, FetchResult, cancel_all, fetch, identity
from .db import KEEP_DAYS, Db
from .models import Listing, Stats, Sub
from .notify import Notifier
from .parser import empty_results, item_ids, parse

log = logging.getLogger(__name__)

AGE_SLACK_MINUTES = 60
BLOCKED_NOTICE = ("Avito подряд отдаёт страницу антибота. Личность и куки стёрты, "
                  "следующий заход сделает новый человек. Если не поможет — нужен "
                  "российский резидентный прокси.")


class Watcher:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.db = Db(cfg.db)
        forgotten = self.db.prune()
        if forgotten:
            log.info("Из «виденного» забыто %d объявлений старше %d дней",
                     forgotten, KEEP_DAYS)
        self.fetch = fetch
        self.notifier = Notifier(cfg.notify, store=Path(cfg.db).parent / "mail.json")
        if self.notifier.configuration_error:
            bad = ", ".join(self.notifier.invalid)
            raise RuntimeError(f"доставка настроена некорректно: {bad}")
        self.state = {"running": False, "started_at": 0.0, "activity": "",
                      "last": "", "error": ""}
        self.errors: dict[str, int] = self.db.error_counts()
        self.empties: dict[str, int] = {}
        self.blocks = 0
        self._lock = threading.Lock()
        self._delivery_lock = threading.Lock()
        self._stop = threading.Event()
        self.sched = BackgroundScheduler(
            job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600})

    def job_id(self, sub_id: str) -> str:
        return f"sub:{sub_id}"

    def sync(self) -> None:
        alive = self.db.subs()
        subs = {s.id: s for s in alive if s.enabled}
        self._sweep_html({s.id for s in alive})
        for job in self.sched.get_jobs():
            if job.id.startswith("sub:") and job.id[4:] not in subs:
                job.remove()
        if self.sched.get_job("delivery") is None:
            self.sched.add_job(self.drain, "interval",
                               seconds=self.cfg.watch.delivery_retry_seconds,
                               jitter=min(self.cfg.watch.jitter_seconds, 60),
                               id="delivery", next_run_time=datetime.now())
        for sub in subs.values():
            seconds = sub.period_minutes * 60
            job = self.sched.get_job(self.job_id(sub.id))
            if job is None:
                self.sched.add_job(
                    self.check, "interval", seconds=seconds,
                    jitter=self.cfg.watch.jitter_seconds, id=self.job_id(sub.id),
                    args=[sub.id], next_run_time=self._first_run(sub))
            elif job.trigger.interval.total_seconds() != seconds:
                job.reschedule("interval", seconds=seconds,
                               jitter=self.cfg.watch.jitter_seconds)

    def _first_run(self, sub: Sub) -> datetime:
        return datetime.fromtimestamp(max(sub.next_at, time.time() + 5))

    def trigger(self, sub_id: str = "") -> None:
        soon = datetime.now()
        for job in self.sched.get_jobs():
            delivery = job.id == "delivery" and not sub_id
            search = job.id.startswith("sub:") and sub_id in ("", job.id[4:])
            if delivery or search:
                job.modify(next_run_time=soon)

    def wanted(self, listings: list[Listing], now: datetime | None = None) -> list[Listing]:
        now = now or datetime.now()
        edge = now - timedelta(minutes=self.cfg.watch.max_age_minutes + AGE_SLACK_MINUTES)
        words = [w.lower() for w in self.cfg.watch.exclude if w.strip()]
        keep = []
        for li in listings:
            # Without a publication date max_age cannot be enforced.  Do not
            # silently treat an undated (possibly old/promoted) card as fresh.
            if li.published_at is None or li.published_at < edge:
                continue
            haystack = f"{li.title} {li.description}".lower()
            if any(word in haystack for word in words):
                continue
            keep.append(li)
        return keep

    def check(self, sub_id: str) -> Stats:
        with self._lock:
            if self._stop.is_set():
                return Stats()
            sub = self.db.sub(sub_id)
            if sub is None or not sub.enabled:
                return Stats()
            stats = self._pass(sub)
            if not self._stop.is_set():
                self._plan(sub, stats)
        return stats

    def save_sub(self, sub: Sub) -> Sub:
        with self._lock:
            old = self.db.sub(sub.id) if sub.id else None
            if sub.id and old is None:
                raise ValueError("такой подписки нет")
            saved = self.db.save(sub)
            if old is not None and old.url != saved.url:
                self.db.reset_sub(saved.id)
        self.sync()
        return saved

    def delete_sub(self, sub_id: str) -> None:
        with self._lock:
            self.db.delete(sub_id)
        self.sync()

    def _pass(self, sub: Sub) -> Stats:
        stats = Stats()
        self.state.update(running=True, started_at=time.time(), error="",
                          activity=f"{sub.name}: захожу на Avito")
        try:
            result = None
            for attempt in range(2):
                if self._stop.is_set():
                    break
                try:
                    result = self._fetch(sub)
                    break
                except Cancelled:
                    break
                except Blocked as exc:
                    stats.errors = 1
                    self._on_error(sub, str(exc), blocked=True)
                    break
                except Exception as exc:
                    if attempt:
                        stats.errors = 1
                        self._on_error(sub, str(exc), blocked=False)
                        break
                    log.warning("[%s] %s — сразу пробую ещё раз", sub.name, exc)
            if result is not None and not self._stop.is_set():
                try:
                    stats = self._handle(sub, result)
                except Exception as exc:
                    log.exception("[%s] падение при разборе", sub.name)
                    stats.errors = 1
                    self._on_error(sub, f"разбор не удался: {exc}", blocked=False)
            if stats.errors and not self._stop.is_set():
                self.drain()
            return stats
        finally:
            self.state.update(running=False, activity="", started_at=0.0, last=str(stats))

    def _fetch(self, sub: Sub) -> FetchResult:
        known = self.db.seen_ids(sub.id, limit=self.cfg.watch.max_per_cycle * 20)
        try:
            if self.fetch is fetch:
                result = self.fetch(self.cfg.browser, sub.url, known_ids=known, detailed=True,
                                    cancel_event=self._stop)
            else:
                result = self.fetch(self.cfg.browser, sub.url, known_ids=known, detailed=True)
        except TypeError:
            result = self.fetch(self.cfg.browser, sub.url)
        if isinstance(result, FetchResult):
            return result
        return FetchResult(str(result))

    def _handle(self, sub: Sub, result: str | FetchResult) -> Stats:
        fetch_result = result if isinstance(result, FetchResult) else FetchResult(result)
        html = fetch_result.html
        self.state["activity"] = f"{sub.name}: разбираю выдачу"
        self._save_html(sub.id, html)
        listings = parse(html)
        if listings:
            cards = len(item_ids(html))
            if cards and len(listings) / cards < 0.9:
                raise RuntimeError(
                    f"разобрано только {len(listings)} из {cards} карточек")
            stats = self._collect(sub, listings)
            if fetch_result.limit_reached:
                log.info("[%s] обход дошёл до лимита %d стр.", sub.name, fetch_result.pages)
            if not stats.errors:
                self._recovered(sub)
            return stats
        stats = Stats()
        if empty_results(html):
            self._recovered(sub)
            log.info("[%s] выдача пуста — по запросу ничего не найдено", sub.name)
        else:
            self._on_empty(sub, stats)
        return stats

    def _recovered(self, sub: Sub) -> None:
        if self.errors.pop(sub.id, None) is not None:
            log.info("[%s] снова работает", sub.name)
            self.db.set_errors(sub.id, 0)
        self.blocks = 0
        self.empties.pop(sub.id, None)

    def _on_empty(self, sub: Sub, stats: Stats) -> None:
        empties = self.empties.get(sub.id, 0) + 1
        self.empties[sub.id] = empties
        stats.errors = 1
        self._on_error(sub, f"страница без объявлений {empties} раз подряд, "
                            f"снимок в data/html/{sub.id}.html", blocked=False)

    def _collect(self, sub: Sub, listings: list[Listing]) -> Stats:
        stats = Stats(parsed=len(listings))
        fresh = self.db.unseen(self.wanted(listings), sub.id)
        stats.fresh = len(fresh)
        if not fresh:
            log.info("[%s] разобрано %d, новых нет", sub.name, stats.parsed)
            stats.sent = self.drain(sub.id)
            return stats

        if sub.last_at <= 0:
            log.info("[%s] знакомство: запоминаем %d объявлений без отправки",
                     sub.name, len(fresh))
            for li in fresh:
                self.db.mark(li, sub.id, False)
            return stats

        limit = self.cfg.watch.max_per_cycle
        # Avito orders cards newest-first.  Process the oldest visible backlog
        # first so a steady stream of new cards cannot starve it forever.
        batch = list(reversed(fresh[-limit:]))
        queued = self.db.enqueue(sub.id, batch, self.notifier.target_keys())
        if len(fresh) > limit:
            log.warning("[%s] новых %d, в очередь поставлено %d, остальные в следующий заход",
                        sub.name, len(fresh), len(queued))
        stats.sent = self.drain(sub.id)
        log.info("[%s] %s", sub.name, stats)
        return stats

    def drain(self, sub_id: str | None = None) -> int:
        with self._delivery_lock:
            total = 0
            subs = [self.db.sub(sub_id)] if sub_id else self.db.subs()
            for sub in [s for s in subs if s and s.enabled]:
                total += self._drain_sub(sub)
            return total

    def _drain_sub(self, sub: Sub) -> int:
        sent = 0
        keys = self.notifier.target_keys()
        for target_key in keys:
            while True:
                claim = self.db.claim(
                    sub.id, target_key,
                    limit=self.cfg.watch.delivery_batch_size,
                    lease_seconds=self.cfg.watch.delivery_lease_seconds)
                if claim is None:
                    break
                self.state["activity"] = f"{sub.name}: отправляю находки"
                if self.notifier.send_to(target_key, claim.listings):
                    sent += self.db.complete(claim.token)
                    continue
                retry_after = min(
                    self.cfg.watch.delivery_retry_seconds * 2 ** max(claim.attempts - 1, 0),
                    self.cfg.watch.max_backoff_seconds)
                self.db.release(claim.token, "доставка не удалась", retry_after)
                break
        return sent

    def _snapshots(self) -> Path:
        return Path(self.cfg.db).parent / "html"

    def _save_html(self, sub_id: str, html: str) -> None:
        folder = self._snapshots()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{sub_id}.html").write_text(html, encoding="utf-8")

    def _sweep_html(self, alive: set[str]) -> None:
        for snap in self._snapshots().glob("*.html"):
            if snap.stem not in alive:
                snap.unlink(missing_ok=True)

    def _on_error(self, sub: Sub, message: str, blocked: bool) -> None:
        count = self.errors.get(sub.id, 0) + 1
        self.errors[sub.id] = count
        self.db.set_errors(sub.id, count)
        self.state["error"] = f"{sub.name}: {message}"
        log.error("[%s] ошибка подряд %d, следующая попытка через %d мин: %s",
                  sub.name, count, self.backoff(sub.id) // 60, message)
        if not blocked:
            return
        self.blocks += 1
        if self.blocks >= self.cfg.browser.retire_after:
            self.blocks = 0
            identity.retire(self.cfg.browser)
            log.warning(BLOCKED_NOTICE)

    def backoff(self, sub_id: str) -> int:
        count = self.errors.get(sub_id, 0)
        if count <= 0:
            return 0
        return min(self.cfg.watch.backoff_seconds * 2 ** (count - 1),
                   self.cfg.watch.max_backoff_seconds)

    def _plan(self, sub: Sub, stats: Stats) -> None:
        job = self.sched.get_job(self.job_id(sub.id))
        pause = self.backoff(sub.id)
        if job is not None and pause:
            job.modify(next_run_time=datetime.now() + timedelta(seconds=pause))
        next_at = job.next_run_time.timestamp() if job and job.next_run_time else 0.0
        self.db.plan(sub.id, next_at=next_at, last_at=time.time() if not stats.errors else None)

    def once(self) -> Stats:
        total = Stats()
        for sub in self.db.subs():
            if sub.enabled:
                total.add(self.check(sub.id))
        return total

    def run(self, handles: list | None = None) -> None:
        self.sync()
        self.sched.start()
        log.info("Подписок в работе: %d, доставка: %s",
                 sum(1 for s in self.db.subs() if s.enabled),
                 ", ".join(self.notifier.schemes) or "только лог")
        try:
            while True:
                time.sleep(5)
                for handle in handles or []:
                    if hasattr(handle, "is_alive") and not handle.is_alive():
                        name = getattr(handle, "name", "управления")
                        raise RuntimeError(f"модуль {name} остановился")
                if not self.sched.running:
                    raise RuntimeError("планировщик остановился")
        except (KeyboardInterrupt, SystemExit):
            log.info("Остановка")

    def close(self) -> None:
        self._stop.set()
        cancel_all()
        if self.sched.running:
            self.sched.shutdown(wait=True)
