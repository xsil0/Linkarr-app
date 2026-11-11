"""Logging for the Logs page: a rotating file in data/logs plus the last lines kept in memory.

MEGA link keys (the part after #) and tokens are masked before anything is written, so a log
can be pasted into a bug report as is.
"""
import logging
import re
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

_MASKS = [
    (re.compile(r"(mega(?:\.co)?\.nz/[^\s#\"']*#)[A-Za-z0-9_\-!]+"), r"\1…"),
    (re.compile(r"(mega://[a-z0-9]+\?)\S+", re.I), r"\1…"),
    (re.compile(r"(bot)\d+:[A-Za-z0-9_\-]{20,}"), r"\1…"),
    (re.compile(r"(X-Plex-Token=|api_key=|sid=)[^&\s]+", re.I), r"\1…"),
]


def mask(text: str) -> str:
    for rx, repl in _MASKS:
        text = rx.sub(repl, text)
    return text


class _Masking(logging.Formatter):
    def format(self, record):
        return mask(super().format(record))


class Ring(logging.Handler):
    def __init__(self, size: int = 1000):
        super().__init__()
        self.lines: deque[str] = deque(maxlen=size)

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:  # never let logging break the app
            pass


RING = Ring()


def setup(log_dir: Path | None) -> None:
    fmt = _Masking("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("linkarr")
    root.setLevel(logging.INFO)
    if any(isinstance(h, Ring) for h in root.handlers):
        return
    RING.setFormatter(fmt)
    root.addHandler(RING)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        f = RotatingFileHandler(log_dir / "linkarr.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        f.setFormatter(fmt)
        root.addHandler(f)
    root.propagate = False
