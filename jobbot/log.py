from __future__ import annotations

import logging
import logging.handlers
import sys

from .config import LOG_DIR

_configured = False


def setup(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    _configured = True
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fh = logging.handlers.RotatingFileHandler(LOG_DIR / "jobbot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
    ch.setLevel(level)
    root.addHandler(ch)

    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "PIL", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(name)
