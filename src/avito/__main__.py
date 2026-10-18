from __future__ import annotations

import logging
import signal
import sys

from . import config, web
from .mail import Mailer
from .service import Service
from .store import Store

log = logging.getLogger("avito")


def main(argv: list[str] | None = None) -> int:
    once = (argv if argv is not None else sys.argv[1:])[:1] == ["once"]
    cfg = config.load()
    config.setup_logging(cfg.log_level)
    store = Store(cfg.db)
    service = Service(cfg, store, Mailer(cfg.mail, store=cfg.db.parent / "mail.json"))
    if once:
        service.cycle()
        return 0
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    web.start(service)
    try:
        service.run()
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановка")
    return 0


if __name__ == "__main__":
    sys.exit(main())
