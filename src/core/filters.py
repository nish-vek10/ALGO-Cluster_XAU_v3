# src/core/filters.py
"""
Trading gate filters.

1. Session filter  — optional time-of-day gate (e.g. London hours only).
2. No-trade zones  — JSON file listing specific date/time windows to pause.

Both filters are checked every loop iteration in main.py BEFORE any orders.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from config.config import (
    USE_SESSION_FILTER, SESSION_START_HHMM, SESSION_END_HHMM,
    USE_NO_TRADE_ZONES, NO_TRADE_ZONES_PATH, LOCAL_TZ,
)
from src.core.logger import log


# ─────────────────────────────────────────────
# SESSION FILTER
# ─────────────────────────────────────────────

def within_session() -> bool:
    """
    Returns True if current London time is within the configured session window.
    Always returns True if USE_SESSION_FILTER is False.
    """
    if not USE_SESSION_FILTER:
        return True

    now = datetime.now(LOCAL_TZ)
    h_s, m_s = map(int, SESSION_START_HHMM.split(":"))
    h_e, m_e = map(int, SESSION_END_HHMM.split(":"))

    start = now.replace(hour=h_s, minute=m_s, second=0, microsecond=0)
    end   = now.replace(hour=h_e, minute=m_e, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)  # overnight session

    return start <= now <= end


# ─────────────────────────────────────────────
# NO-TRADE ZONES
# ─────────────────────────────────────────────

def load_no_trade_zones() -> List[dict]:
    """
    Read no_trade_zones.json.
    Expected format:
      [
        {
          "start_local": "2025-12-10 08:00",
          "end_local":   "2025-12-10 10:00",
          "reason":      "FOMC"
        },
        ...
      ]
    Times are interpreted in Europe/London timezone.
    Returns empty list if file missing or USE_NO_TRADE_ZONES is False.
    """
    if not USE_NO_TRADE_ZONES:
        return []
    if not NO_TRADE_ZONES_PATH.exists():
        return []
    try:
        data = json.loads(NO_TRADE_ZONES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        log(f"[FILTERS] Failed to load no_trade_zones.json: {e}", level="WARN")
        return []


def check_no_trade_zone(
    now_utc: datetime,
    zones: List[dict],
) -> Tuple[bool, Optional[str]]:
    """
    Returns (in_zone: bool, reason: str|None).
    Checks current UTC time against each configured zone window.
    """
    now_local = now_utc.astimezone(LOCAL_TZ)

    for z in zones:
        try:
            start = datetime.strptime(z["start_local"], "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
            end   = datetime.strptime(z["end_local"],   "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
            if start <= now_local <= end:
                return True, z.get("reason", "no_trade_zone")
        except Exception:
            continue   # malformed entry — skip silently

    return False, None
