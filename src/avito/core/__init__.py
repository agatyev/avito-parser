from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

from ..config import Browser as BrowserConfig

log = logging.getLogger(__name__)

BLOCKED = 3
TIMEOUT = 4
CHILD = [sys.executable, "-m", "avito.core"]
PROTOCOL_FLAG = "--pages-v1"
PROTOCOL_MAGIC = "AVITO_FETCH_V1 "
DEFAULT_MAX_PAGES = 3


class Blocked(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


_ACTIVE: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()


def _terminate(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def cancel_all() -> None:
    """Stop current browser children during shutdown."""
    with _ACTIVE_LOCK:
        active = tuple(_ACTIVE)
    for proc in active:
        _terminate(proc)


@dataclass(frozen=True, slots=True)
class FetchResult:
    html: str
    pages: int = 1
    stop_reason: str = "single"
    limit_reached: bool = False


def _echo(stream: bytes) -> list[str]:
    lines = [line for line in stream.decode("utf-8", "replace").splitlines() if line.strip()]
    for line in lines:
        log.info("| %s", line)
    return lines


def _tail(lines: list[str]) -> str:
    return " | ".join(lines[-5:]) if lines else "браузер молчал"


def _request(known_ids: Iterable[str], max_pages: int) -> bytes:
    payload = {"known_ids": sorted({str(item_id) for item_id in known_ids}),
               "max_pages": max_pages}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _result(stream: bytes, protocol: bool) -> FetchResult:
    if protocol:
        header, separator, html = stream.partition(b"\n")
        prefix = PROTOCOL_MAGIC.encode()
        if separator and header.startswith(prefix):
            meta = json.loads(header[len(prefix):])
            return FetchResult(
                html.decode("utf-8", "replace"),
                pages=max(int(meta.get("pages", 1)), 1),
                stop_reason=str(meta.get("stop_reason") or "unknown"),
                limit_reached=bool(meta.get("limit_reached", False)),
            )
        raise RuntimeError("ядро не вернуло метаданные прохода")
    return FetchResult(stream.decode("utf-8", "replace"))


def fetch(cfg: BrowserConfig, link: str, known_ids: Iterable[str] = (),
          detailed: bool = False,
          cancel_event: threading.Event | None = None) -> str | FetchResult:
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled("сервис останавливается")
    known = tuple(known_ids)
    protocol = detailed or bool(known)
    args = [*CHILD, link, *([PROTOCOL_FLAG] if protocol else [])]
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
              "start_new_session": True}
    if protocol:
        kwargs["stdin"] = subprocess.PIPE
    proc = subprocess.Popen(args, **kwargs)
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)
        cancelling = cancel_event is not None and cancel_event.is_set()
    if cancelling:
        _terminate(proc)
    started = time.monotonic()
    try:
        max_pages = int(getattr(cfg, "max_pages", DEFAULT_MAX_PAGES))
        payload = _request(known, max_pages) if protocol else None
        out, err = proc.communicate(payload, timeout=cfg.deadline_seconds)
    except subprocess.TimeoutExpired:
        _terminate(proc)
        last = _echo(proc.communicate()[1])
        raise TimeoutError(f"заход не уложился в {cfg.deadline_seconds} с, "
                           f"браузер успел: {_tail(last)}") from None
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(proc)
    if cancel_event is not None and cancel_event.is_set() and proc.returncode:
        raise Cancelled("сервис останавливается")
    log.info("Заход занял %d с", time.monotonic() - started)
    last = _echo(err)
    if proc.returncode == BLOCKED:
        raise Blocked(last[-1] if last else "антибот Avito")
    if proc.returncode == TIMEOUT:
        raise TimeoutError(last[-1] if last else "шаг захода не ответил")
    if proc.returncode:
        raise RuntimeError(last[-1] if last else f"заход упал, код {proc.returncode}")
    result = _result(out, protocol)
    return result if detailed else result.html
