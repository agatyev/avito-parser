from __future__ import annotations

import json
import sys

from ..config import load, setup_logging
from . import BLOCKED, PROTOCOL_FLAG, PROTOCOL_MAGIC, TIMEOUT, Blocked, FetchResult


def _request() -> tuple[set[str], int]:
    data = json.load(sys.stdin)
    known = data.get("known_ids", [])
    if not isinstance(known, list):
        raise ValueError("known_ids должен быть списком")
    return {str(item_id) for item_id in known}, max(1, min(int(data["max_pages"]), 50))


def _write(result: FetchResult) -> None:
    meta = {"pages": result.pages, "stop_reason": result.stop_reason,
            "limit_reached": result.limit_reached}
    sys.stdout.write(PROTOCOL_MAGIC + json.dumps(meta, separators=(",", ":")) + "\n")
    sys.stdout.write(result.html)


def main() -> int:
    cfg = load()
    setup_logging(cfg.log_level)
    from .session import grab

    try:
        protocol = sys.argv[2:] == [PROTOCOL_FLAG]
        if protocol:
            known_ids, max_pages = _request()
            result = grab(cfg.browser, sys.argv[1], known_ids=known_ids,
                          max_pages=max_pages, detailed=True)
            _write(result)
        else:
            sys.stdout.write(grab(cfg.browser, sys.argv[1]))
    except Blocked as exc:
        print(exc, file=sys.stderr)
        return BLOCKED
    except TimeoutError as exc:
        print(exc, file=sys.stderr)
        return TIMEOUT
    return 0


if __name__ == "__main__":
    sys.exit(main())
