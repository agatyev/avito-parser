from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from conftest import FakeFetch, FakeNotifier

from avito.core import Blocked, identity
from avito.models import Listing, Sub
from avito.watch import Watcher


def add_sub(watcher, **kwargs) -> Sub:
    fields = {"id": "", "name": "Квартиры", "url": "https://www.avito.ru/moskva/kvartiry"}
    return watcher.db.save(Sub(**{**fields, **kwargs}))


def listing(item_id: str, **kwargs) -> Listing:
    fields = {"url": f"https://www.avito.ru/items/{item_id}", "title": f"Лот {item_id}",
              "published_at": datetime.now()}
    return Listing(id=item_id, **{**fields, **kwargs})


def test_first_pass_remembers_without_sending(watcher):
    sub = add_sub(watcher)
    stats = watcher.check(sub.id)

    assert stats.parsed == 6 and stats.fresh == 4 and stats.sent == 0
    assert watcher.notifier.batches == []
    assert watcher.db.count() == 4
    assert watcher.db.sub(sub.id).last_at > 0


def test_pass_keeps_the_raw_page(watcher, html):
    sub = add_sub(watcher)
    watcher.check(sub.id)

    saved = watcher.cfg.db.parent / "html" / f"{sub.id}.html"
    assert saved.read_text(encoding="utf-8") == html


def test_next_pass_sends_only_new(watcher):
    sub = add_sub(watcher)
    watcher.check(sub.id)
    fresh = watcher.db.sub(sub.id)

    stats = watcher._collect(fresh, [listing("999", price=100)])

    assert (stats.fresh, stats.sent) == (1, 1)
    assert [li.id for li in watcher.notifier.batches[0]] == ["999"]
    assert watcher.db.recent()[0]["sent"] == 1


def test_undelivered_listing_is_kept_new_until_it_is_delivered(watcher):
    watcher.notifier = FakeNotifier(ok=False)
    sub = add_sub(watcher)
    watcher.db.plan(sub.id, next_at=0, last_at=1.0)

    stats = watcher._collect(watcher.db.sub(sub.id), [listing("999")])

    assert stats.sent == 0 and stats.errors == 0
    assert watcher.backoff(sub.id) == 0
    assert watcher.db.count() == 1
    assert watcher.db.pending(sub.id) == 1
    assert watcher.db.delivery_status()["failed"] == 1

    watcher.notifier = FakeNotifier(ok=True)
    with watcher.db.connect() as conn:
        conn.execute("UPDATE outbox_targets SET lease_until = 0")
    stats = watcher._collect(watcher.db.sub(sub.id), [listing("999")])

    assert stats.sent == 1
    assert [li.id for li in watcher.notifier.batches[0]] == ["999"]
    assert watcher.db.recent()[0]["sent"] == 1


def test_batch_processes_the_oldest_backlog_first(watcher):
    watcher.cfg.watch.max_per_cycle = 2
    sub = add_sub(watcher)
    watcher.db.plan(sub.id, next_at=0, last_at=1.0)
    page = [listing("3"), listing("2"), listing("1")]

    stats = watcher._collect(watcher.db.sub(sub.id), page)

    assert stats.sent == 2
    assert [li.id for li in watcher.notifier.batches[0]] == ["1", "2"]
    assert {r["id"]: r["sent"] for r in watcher.db.recent()} == {"1": 1, "2": 1}

    stats = watcher._collect(watcher.db.sub(sub.id), page)

    assert [li.id for li in watcher.notifier.batches[1]] == ["3"]
    assert {r["id"] for r in watcher.db.recent()} == {"1", "2", "3"}


def test_wanted_drops_stale_and_excluded(watcher):
    now = datetime(2026, 8, 19, 12, 0)
    watcher.cfg.watch.max_age_minutes = 60
    watcher.cfg.watch.exclude = ["Апартаменты"]
    page = [
        listing("fresh", published_at=now - timedelta(minutes=30)),
        listing("stale", published_at=now - timedelta(days=3)),
        listing("word", title="Продам апартаменты", published_at=now),
        listing("undated", published_at=None),
    ]

    assert [li.id for li in watcher.wanted(page, now)] == ["fresh"]


def test_one_browser_hiccup_retries_at_once_without_a_pause(watcher, html):
    sub = add_sub(watcher)
    calls = {"n": 0}

    def flaky(cfg, link):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Target page, context or browser has been closed")
        return html

    watcher.fetch = flaky
    stats = watcher.check(sub.id)

    assert calls["n"] == 2
    assert stats.errors == 0
    assert watcher.backoff(sub.id) == 0


def test_errors_grow_the_pause_and_success_resets_it(watcher, html):
    sub = add_sub(watcher)
    watcher.fetch = FakeFetch(error=RuntimeError("сеть отвалилась"))

    assert watcher.check(sub.id).errors == 1
    assert watcher.backoff(sub.id) == watcher.cfg.watch.backoff_seconds
    watcher.check(sub.id)
    assert watcher.backoff(sub.id) == watcher.cfg.watch.backoff_seconds * 2

    watcher.fetch = FakeFetch(html)
    watcher.check(sub.id)
    assert watcher.backoff(sub.id) == 0


def test_chronic_failures_stay_in_logs(watcher, html, caplog):
    sub = add_sub(watcher)
    watcher.fetch = FakeFetch(error=RuntimeError("сеть отвалилась"))

    with caplog.at_level(logging.WARNING):
        watcher.check(sub.id)
        watcher.check(sub.id)
        watcher.check(sub.id)

    assert watcher.notifier.batches == []
    assert "сеть отвалилась" in caplog.text

    watcher.fetch = FakeFetch(html)
    with caplog.at_level(logging.INFO):
        watcher.check(sub.id)
    assert "снова работает" in caplog.text
    assert watcher.notifier.batches == []


def test_parser_crash_counts_as_error(watcher, html, monkeypatch, caplog):
    sub = add_sub(watcher)
    watcher.db.plan(sub.id, next_at=0, last_at=1.0)
    calls = {"n": 0}

    def flaky(text, now=None):
        if calls["n"] < 2:
            calls["n"] += 1
            raise RuntimeError("вёрстка поменялась")
        return [listing(str(item_id)) for item_id in range(6)]

    monkeypatch.setattr("avito.watch.parse", flaky)
    with caplog.at_level(logging.ERROR):
        watcher.check(sub.id)
        stats = watcher.check(sub.id)

    assert stats.errors == 1
    assert watcher.notifier.batches == []
    assert "вёрстка поменялась" in caplog.text

    stats = watcher.check(sub.id)
    assert stats.errors == 0


def test_empty_pages_warn_once_then_back_off_until_recovery(watcher, html):
    sub = add_sub(watcher)
    watcher.fetch = FakeFetch("<html></html>")

    watcher.check(sub.id)
    assert watcher.notifier.batches == []
    assert watcher.backoff(sub.id) == watcher.cfg.watch.backoff_seconds
    watcher.check(sub.id)
    assert watcher.backoff(sub.id) == watcher.cfg.watch.backoff_seconds * 2

    watcher.check(sub.id)
    assert watcher.notifier.batches == []

    watcher.fetch = FakeFetch(html)
    watcher.check(sub.id)
    assert watcher.backoff(sub.id) == 0

    watcher.fetch = FakeFetch("<html></html>")
    watcher.check(sub.id)
    watcher.check(sub.id)
    assert watcher.notifier.batches == []


def test_backoff_has_a_ceiling(watcher):
    watcher.errors["x"] = 99
    assert watcher.backoff("x") == watcher.cfg.watch.max_backoff_seconds


def test_backoff_survives_a_restart(settings, html):
    first = Watcher(settings)
    sub = first.db.save(Sub(id="", name="Квартиры", url="https://www.avito.ru/moskva"))
    first.fetch = FakeFetch(error=RuntimeError("сеть"))
    first.check(sub.id)
    first.close()

    second = Watcher(settings)
    try:
        assert second.backoff(sub.id) == settings.watch.backoff_seconds
        second.fetch = FakeFetch(html)
        second.check(sub.id)
        assert second.backoff(sub.id) == 0
    finally:
        second.close()


def test_legitimate_empty_page_is_not_an_anomaly(watcher):
    sub = add_sub(watcher)
    watcher.fetch = FakeFetch("<html><body>По вашему запросу ничего не найдено</body></html>")

    stats = watcher.check(sub.id)

    assert stats.errors == 0 and stats.parsed == 0
    assert watcher.notifier.batches == []
    assert watcher.backoff(sub.id) == 0


def test_antibot_is_reported_once(watcher, caplog):
    sub = add_sub(watcher)
    watcher.fetch = FakeFetch(error=Blocked("HTTP 403"))

    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            watcher.check(sub.id)

    assert watcher.notifier.batches == []
    assert caplog.text.count("Личность и куки стёрты") == 1


def test_disabled_subscription_is_skipped(watcher):
    sub = add_sub(watcher, enabled=False)
    assert watcher.check(sub.id).parsed == 0
    assert watcher.once().parsed == 0


def test_every_pass_leaves_the_service_idle(watcher):
    sub = add_sub(watcher)
    watcher.check(sub.id)
    watcher.check(sub.id)
    assert watcher.fetch.calls == 2
    assert watcher.state["running"] is False and watcher.state["activity"] == ""


def test_url_change_waits_for_old_pass_and_clears_its_results(watcher, html):
    sub = add_sub(watcher)
    started = threading.Event()
    release = threading.Event()

    def slow_fetch(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return html

    watcher.fetch = slow_fetch
    check = threading.Thread(target=watcher.check, args=(sub.id,))
    check.start()
    assert started.wait(1)

    changed = threading.Thread(target=watcher.save_sub, args=(Sub(
        id=sub.id, name=sub.name, url="https://www.avito.ru/spb/kvartiry",
        period_minutes=sub.period_minutes),))
    changed.start()
    assert changed.is_alive()
    release.set()
    check.join(2)
    changed.join(2)

    assert not check.is_alive() and not changed.is_alive()
    assert watcher.db.sub(sub.id).url == "https://www.avito.ru/spb/kvartiry"
    assert watcher.db.sub(sub.id).last_at == 0
    assert watcher.db.count() == 0


def test_partial_parser_result_is_an_error(watcher, monkeypatch):
    sub = add_sub(watcher)
    watcher.fetch = FakeFetch("".join(
        f'<div data-marker="item" data-item-id="{n}"></div>' for n in range(10)))
    monkeypatch.setattr("avito.watch.parse", lambda html: [listing("1")])

    stats = watcher.check(sub.id)

    assert stats.errors == 1
    assert "1 из 10" in watcher.state["error"]


def test_identity_is_retired_after_three_blocks(watcher):
    sub = add_sub(watcher)
    watcher.fetch = FakeFetch(error=Blocked("HTTP 403"))
    home = identity.home(watcher.cfg.browser)
    home.mkdir(parents=True, exist_ok=True)
    (home / "identity.json").write_text("{}")

    watcher.check(sub.id)
    watcher.check(sub.id)
    assert home.exists()

    watcher.check(sub.id)
    assert home.exists()
    assert not (home / "identity.json").exists()


def test_sync_adds_removes_and_reschedules_jobs(watcher):
    first = add_sub(watcher)
    watcher.sync()
    assert watcher.sched.get_job(watcher.job_id(first.id)) is not None

    watcher.db.save(Sub(id=first.id, name="Квартиры", url=first.url, period_minutes=180))
    watcher.sync()
    job = watcher.sched.get_job(watcher.job_id(first.id))
    assert job.trigger.interval.total_seconds() == 180 * 60

    watcher.db.delete(first.id)
    watcher.sync()
    assert watcher.sched.get_job(watcher.job_id(first.id)) is None


def test_sync_sweeps_snapshots_of_deleted_subscriptions(watcher):
    sub = add_sub(watcher)
    folder = watcher._snapshots()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{sub.id}.html").write_text("живой")
    (folder / "сгинувшая.html").write_text("сирота")

    watcher.sync()

    assert (folder / f"{sub.id}.html").exists()
    assert not (folder / "сгинувшая.html").exists()

    watcher.db.delete(sub.id)
    watcher.sync()
    assert not (folder / f"{sub.id}.html").exists()


def test_sync_and_trigger_on_a_running_scheduler(watcher):
    sub = add_sub(watcher)
    watcher.sched.start()
    try:
        watcher.sync()
        job = watcher.sched.get_job(watcher.job_id(sub.id))
        assert job is not None
        assert job.trigger.interval.total_seconds() == 60 * 60

        planned = job.next_run_time
        watcher.trigger(sub.id)
        job = watcher.sched.get_job(watcher.job_id(sub.id))
        assert job.next_run_time < planned
    finally:
        watcher.sched.shutdown(wait=False)
