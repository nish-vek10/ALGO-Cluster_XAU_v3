# src/core/logger.py
"""
Logging infrastructure.

FIX vs original script:
  The original had a double-logging bug: log_event() called _append_jsonl()
  directly AND called print(), which was intercepted by StdTeeToJsonl and
  written to JSONL again — every structured log line appeared twice in the file.

Solution here:
  - log_event() writes structured JSONL directly via _append_jsonl(),
    and writes human-readable output directly to sys.__stdout__ (bypasses Tee).
  - StdTeeToJsonl is kept ONLY to capture raw print() calls from third-party
    code, exceptions, or any code path that does not go through log_event().
  - This ensures each structured log line appears exactly once in the file.
"""
from __future__ import annotations

import sys
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from pytz import timezone

if TYPE_CHECKING:
    from src.core.models import StrategyConfig

# Filled by init_logger() — avoids circular import from config
_BOT_NAME: str = "XAU_Bot"
_LOG_PATH: Optional[Path] = None


def init_logger(bot_name: str, log_path: Path) -> None:
    """Call once at startup before any logging."""
    global _BOT_NAME, _LOG_PATH
    _BOT_NAME = bot_name
    _LOG_PATH = log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────
# LOW-LEVEL FILE WRITER
# ─────────────────────────────────────────────

def _append_jsonl(obj: dict) -> None:
    """Append one JSON object as a line to the log file. Never raises."""
    if _LOG_PATH is None:
        return
    try:
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            f.flush()
    except Exception as e:
        # Never crash the bot due to a logging failure
        sys.__stdout__.write(f"[LOGGER][WARN] write failed: {e}\n")


# ─────────────────────────────────────────────
# MAIN LOG FUNCTION
# ─────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone("UTC"))


def log_event(
    level: str,
    msg: str,
    *,
    cfg: Optional["StrategyConfig"] = None,
    **fields,
) -> None:
    """
    Unified structured logger.
    - Writes JSON line to log file (once).
    - Writes human-readable line to real terminal (bypassing StdTeeToJsonl).
    """
    ts  = _utc_now()
    ts_iso = ts.isoformat()

    payload = {
        "ts_utc":   ts_iso,
        "level":    level.upper(),
        "bot":      _BOT_NAME,
        "strategy": cfg.name  if cfg else None,
        "magic":    cfg.magic if cfg else None,
        "msg":      msg,
        **fields,
    }

    # ── File (structured JSONL) ──────────────────────────────────────────
    _append_jsonl(payload)

    # ── Terminal (human-readable, written to real stdout — NOT through Tee)
    prefix = f"[{ts.strftime('%Y-%m-%d %H:%M:%S')} UTC] [{payload['level']}]"
    line   = f"{prefix} [{cfg.name}] {msg}" if cfg else f"{prefix} {msg}"
    sys.__stdout__.write(line + "\n")
    sys.__stdout__.flush()


def log(msg: str, level: str = "INFO", **fields) -> None:
    """Log without strategy context."""
    log_event(level, msg, cfg=None, **fields)


def log_strategy(cfg: "StrategyConfig", msg: str, level: str = "INFO", **fields) -> None:
    """Log with strategy context (name + magic appended)."""
    log_event(level, msg, cfg=cfg, **fields)


# ─────────────────────────────────────────────
# STD TEE  (captures stray print() calls only)
# ─────────────────────────────────────────────

class StdTeeToJsonl:
    """
    Redirects sys.stdout / sys.stderr so that any raw print() call
    (e.g. from MetaTrader5 library, unhandled exceptions) is also stored
    in the JSONL log.

    Does NOT write structured log_event() output — that path bypasses Tee
    by writing directly to sys.__stdout__.
    """

    def __init__(self, real_stream, level: str):
        self.real_stream = real_stream
        self.level = level.upper()
        self._buf  = ""

    def write(self, s: str) -> int:
        # Always pass through to real terminal
        n = self.real_stream.write(s)
        self.real_stream.flush()

        # Buffer until newline, then persist complete lines
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line.strip():
                _append_jsonl({
                    "ts_utc":   _utc_now().isoformat(),
                    "level":    self.level,
                    "bot":      _BOT_NAME,
                    "strategy": None,
                    "magic":    None,
                    "msg":      line,
                    "source":   "tee_stdout" if self.level == "INFO" else "tee_stderr",
                })
        return n

    def flush(self) -> None:
        self.real_stream.flush()
        # Flush any partial line in buffer
        if self._buf.strip():
            _append_jsonl({
                "ts_utc":   _utc_now().isoformat(),
                "level":    self.level,
                "bot":      _BOT_NAME,
                "strategy": None,
                "magic":    None,
                "msg":      self._buf.rstrip("\r"),
                "source":   "tee_flush",
            })
            self._buf = ""

    def isatty(self):
        return getattr(self.real_stream, "isatty", lambda: False)()

    def fileno(self):
        return getattr(self.real_stream, "fileno", lambda: -1)()


def enable_print_capture() -> None:
    """Redirect sys.stdout/stderr through StdTeeToJsonl (call once at startup)."""
    sys.stdout = StdTeeToJsonl(sys.__stdout__, level="INFO")
    sys.stderr = StdTeeToJsonl(sys.__stderr__, level="ERROR")
