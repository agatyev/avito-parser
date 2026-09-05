from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import urllib.request

from . import config, control
from .config import setup_logging
from .watch import Watcher

STUCK_SECONDS = 1200
DEAD_ERRORS = 3


def cmd_run(cfg) -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    watcher = Watcher(cfg)
    handles = control.start(cfg.control, watcher)
    try:
        watcher.run(handles)
    finally:
        watcher.close()
    return 0


def cmd_once(cfg) -> int:
    watcher = Watcher(cfg)
    try:
        stats = watcher.once()
    finally:
        watcher.close()
    print(stats)
    return 1 if stats.errors else 0


def cmd_health(cfg) -> int:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{cfg.web.port}/api/state", timeout=10) as response:
            state = json.load(response)
    except Exception as exc:
        print(f"страница не отвечает: {exc}")
        return 1
    status = state["status"]
    started = float(status.get("started_at") or 0)
    stuck = time.time() - started if started else 0
    stuck_seconds = max(STUCK_SECONDS, cfg.browser.deadline_seconds * 2 + 60)
    if status.get("running") and stuck > stuck_seconds:
        print(f"проход висит {int(stuck // 60)} мин: {status.get('activity')}")
        return 1
    live = [s for s in state["searches"] if s["enabled"]]
    failing = [s for s in live if s["errors"] >= DEAD_ERRORS]
    if failing:
        print("подписки не работают: " + ", ".join(s["name"] for s in failing))
        return 1
    pending = int(state.get("queue", {}).get("pending", 0))
    queue = state.get("queue", {})
    failed = int(queue.get("failed", 0))
    unavailable = queue.get("unavailable_targets") or []
    oldest_at = float(queue.get("oldest_at") or 0)
    oldest_age = time.time() - oldest_at if oldest_at else 0
    stale_after = max(cfg.watch.delivery_retry_seconds * 3,
                      cfg.watch.delivery_lease_seconds * 2, 900)
    if pending and (failed or unavailable or oldest_age > stale_after):
        reason = (f"ошибок {failed}" if failed else
                  f"нет targets: {', '.join(unavailable)}" if unavailable else
                  f"старше {int(oldest_age // 60)} мин")
        print(f"доставка не здорова: {pending} задач, {reason}")
        return 1
    print(f"жив, {'идёт проход' if status.get('running') else 'ждёт'}")
    return 0


COMMANDS = {"run": cmd_run, "once": cmd_once, "health": cmd_health}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="avito", description="Слежу за выдачей Avito")
    parser.add_argument("command", choices=COMMANDS, help="run — следить, once — один проход, "
                                                          "health — жив ли сервис")
    args = parser.parse_args(argv)
    cfg = config.load()
    history = cfg.db.parent / "service.log" if args.command != "health" else None
    setup_logging(cfg.log_level, history)
    return COMMANDS[args.command](cfg)
