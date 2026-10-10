"""Structured key=value logs to console and a rotating file. Secrets are redacted in a filter."""
from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import time
from pathlib import Path

log = logging.getLogger("copybot")
_SECRETS: list[str] = []
_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{25,}")  # a bot token shape (kept: redacting more never hurts)


def add_secret(s: str) -> None:
    if s and len(s) >= 3 and s not in _SECRETS:
        _SECRETS.append(s)


def redact(text: str) -> str:
    for s in _SECRETS:
        text = text.replace(s, "***")
    return _TOKEN_RE.sub("***", text)


class _Redact(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = redact(record.getMessage())
        if record.exc_info:
            msg += "\n" + redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
            record.exc_text = None
        record.msg, record.args = msg, ()
        return True


class _Fmt(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z"


def _val(v) -> str:
    if isinstance(v, float):
        v = f"{v:.6g}"
    s = str(v)
    if s == "" or any(c in s for c in ' ="\n'):
        s = '"' + s.replace('"', "'").replace("\n", " | ") + '"'
    return s


def kv(event: str, **fields) -> str:
    return " ".join([f"event={event}"] + [f"{k}={_val(v)}" for k, v in fields.items()])


def info(event: str, **fields) -> None:
    log.info(kv(event, **fields))


def warn(event: str, **fields) -> None:
    log.warning(kv(event, **fields))


def error(event: str, **fields) -> None:
    log.error(kv(event, **fields))


def exception(event: str, **fields) -> None:
    log.error(kv(event, **fields), exc_info=True)


def setup(log_dir: str | None, level: int = logging.INFO) -> None:
    log.setLevel(level)
    log.propagate = False
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()
    fmt = _Fmt("%(asctime)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            Path(log_dir) / "copybot.log", maxBytes=10_000_000, backupCount=10, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(_Redact())
        log.addHandler(h)
