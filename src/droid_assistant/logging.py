"""Structured logging with per-component levels, adjustable at runtime (NFR-MNT-3).

Two formats: a human one for a terminal, JSON when `DROID_LOG_JSON=1` or output
is not a TTY, so `journalctl` and a docker log driver both get parseable lines.

Nothing here ever logs a credential: config stores only variable *names*
(FR-CFG-4), and `redact()` is a second line of defence for strings assembled
elsewhere, such as a backend URL carrying a query-string key.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from typing import Any, ClassVar

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{12,})"),
    re.compile(r"(?i)\b(?:api[-_]?key|token|authorization|secret)\b\s*[=:]\s*['\"]?([^\s'\",;]+)"),
    re.compile(r"(?i)([?&](?:key|token|api_key|access_token)=)([^&\s]+)"),
)

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(
            lambda m: m.group(0).replace(m.group(m.lastindex or 1), "***"),
            text,
        )
    return text


class HumanFormatter(logging.Formatter):
    _COLOURS: ClassVar[dict[str, str]] = {
        "DEBUG": "\033[2;37m",
        "INFO": "\033[36m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    _RESET = "\033[0m"

    def __init__(self, colour: bool) -> None:
        super().__init__()
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        level = record.levelname
        tint, reset = (self._COLOURS.get(level, ""), self._RESET) if self.colour else ("", "")
        name = record.name.removeprefix("droid_assistant.")
        extras = " ".join(
            f"{k}={v}"
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        )
        line = f"{stamp} {tint}{level:<7}{reset} {name:<28} {redact(record.getMessage())}"
        if extras:
            line += f"  {tint}{redact(extras)}{reset}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = (
                    value if isinstance(value, str | int | float | bool | None) else str(value)
                )
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure(level: str = "info", *, json_output: bool | None = None) -> None:
    """Install the root handler. Idempotent — safe to call from tests and the CLI."""
    if json_output is None:
        json_output = os.environ.get("DROID_LOG_JSON") == "1" or not sys.stderr.isatty()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JSONFormatter() if json_output else HumanFormatter(sys.stderr.isatty()))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(logging.WARNING)  # third-party libraries stay quiet by default
    logging.getLogger("droid_assistant").setLevel(level.upper())

    # Per-component overrides: DROID_LOG_PIPELINE=debug → droid_assistant.pipeline
    for key, value in os.environ.items():
        if key.startswith("DROID_LOG_") and key not in {"DROID_LOG_JSON", "DROID_LOG_LEVEL"}:
            component = key.removeprefix("DROID_LOG_").lower()
            logging.getLogger(f"droid_assistant.{component}").setLevel(value.upper())


def set_level(component: str, level: str) -> None:
    """Runtime adjustment, exposed through `PATCH /api/config` (NFR-MNT-3)."""
    name = "droid_assistant" if component in {"", "root", "all"} else f"droid_assistant.{component}"
    logging.getLogger(name).setLevel(level.upper())


def current_levels() -> dict[str, str]:
    manager = logging.Logger.manager
    out: dict[str, str] = {}
    for name, logger in manager.loggerDict.items():
        if (
            name.startswith("droid_assistant")
            and isinstance(logger, logging.Logger)
            and logger.level
        ):
            out[name] = logging.getLevelName(logger.level).lower()
    return out
