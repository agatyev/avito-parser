from __future__ import annotations

import importlib
import logging

log = logging.getLogger(__name__)


def start(names: list[str], watcher) -> list:
    running = []
    for name in names:
        try:
            running.append(importlib.import_module(f".{name}", __package__).start(watcher))
        except Exception as exc:
            raise RuntimeError(f"модуль управления «{name}» не поднялся: {exc}") from exc
    return running
