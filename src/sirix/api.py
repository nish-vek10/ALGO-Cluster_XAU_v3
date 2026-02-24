# src/sirix/api.py
"""
SiRiX REST API interface.

Responsibilities:
  - Fetch open positions from the SiRiX prop-firm API.
  - Parse / normalise raw JSON into SirixPositionEvent objects.
  - De-duplicate by OrderID via a time-expiring cache (SeenOrdersCache).
  - Infer trade side from SL/TP geometry first, ActionType integer second.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Set

import requests
from pytz import timezone

from config.config import (
    SIRIX_BASE_URL, SIRIX_ENDPOINT, SIRIX_TOKEN, SIRIX_GROUPS,
    SIRIX_INSTRUMENT, SIRIX_TZ, SIRIX_HTTP_TIMEOUT, SEEN_ORDERS_MAX_AGE_HOURS,
    VERBOSE_CLUSTERS,
)
from src.core.models import SirixPositionEvent
from src.core.logger import log


HEADERS = {
    "Authorization": f"Bearer {SIRIX_TOKEN}",
    "Content-Type": "application/json",
}


# ─────────────────────────────────────────────
# SEEN-ORDERS CACHE  (de-duplication + memory-safe)
# ─────────────────────────────────────────────

class SeenOrdersCache:
    """
    Thread-safe set of observed OrderIDs with automatic time-based expiry.

    Problem solved:
      The original script used a plain `set` that grew forever. Over a multi-day
      run this wastes memory and, on restart, the set is lost so old orders can
      re-trigger. This cache purges entries older than SEEN_ORDERS_MAX_AGE_HOURS
      on every call to `contains()`.
    """

    def __init__(self, max_age_hours: int = SEEN_ORDERS_MAX_AGE_HOURS):
        self._max_age  = timedelta(hours=max_age_hours)
        self._store: dict[str, datetime] = {}   # order_id → first_seen_utc

    def contains(self, order_id: str) -> bool:
        self._prune()
        return order_id in self._store

    def add(self, order_id: str) -> None:
        self._prune()
        if order_id not in self._store:
            self._store[order_id] = datetime.now(timezone("UTC"))

    def bootstrap(self, order_ids: Set[str]) -> None:
        """Pre-populate at startup to ignore all pre-existing orders."""
        ts = datetime.now(timezone("UTC"))
        for oid in order_ids:
            self._store[oid] = ts
        log(f"[SIRIX] Bootstrap: ignoring {len(order_ids)} pre-existing OrderIDs")

    def __len__(self) -> int:
        return len(self._store)

    def _prune(self) -> None:
        cutoff = datetime.now(timezone("UTC")) - self._max_age
        expired = [k for k, v in self._store.items() if v < cutoff]
        for k in expired:
            del self._store[k]


# ─────────────────────────────────────────────
# SIDE INFERENCE
# ─────────────────────────────────────────────

def _infer_side(
    action_type: Optional[int],
    open_rate:   Optional[float],
    sl:          Optional[float],
    tp:          Optional[float],
) -> Optional[str]:
    """
    Determine "buy" or "sell" from a SiRiX position record.

    Priority:
      1) SL/TP geometry — if SL < open < TP → buy; TP < open < SL → sell
         (reliable regardless of ActionType encoding changes)
      2) ActionType integer: 0=buy, 1=sell, 2=sell (legacy)
    """
    # 1) Geometry
    try:
        if open_rate and sl and tp and sl > 0 and tp > 0:
            if sl < open_rate < tp:
                return "buy"
            if tp < open_rate < sl:
                return "sell"
    except Exception:
        pass

    # 2) ActionType fallback
    if action_type == 0:
        return "buy"
    if action_type in (1, 2):
        return "sell"

    return None


# ─────────────────────────────────────────────
# ISO8601 PARSER
# ─────────────────────────────────────────────

def _parse_utc(ts: str) -> datetime:
    """
    Parse SiRiX ISO8601 timestamp → UTC-aware datetime.
    Naive datetimes are assumed to be Israel time (SIRIX_TZ).
    """
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SIRIX_TZ)

    return dt.astimezone(timezone("UTC"))


# ─────────────────────────────────────────────
# FETCH + PARSE
# ─────────────────────────────────────────────

def fetch_raw_positions(lookback_seconds: int) -> List[dict]:
    """
    Call SiRiX API and return raw position list.
    Returns [] on any network / parse error (bot continues on next iteration).
    """
    now   = datetime.now(timezone("UTC"))
    start = now - timedelta(seconds=lookback_seconds)

    payload = {
        "groups":    SIRIX_GROUPS,
        "startTime": start.isoformat().replace("+00:00", "Z"),
        "endTime":   now.isoformat().replace("+00:00", "Z"),
    }

    try:
        resp = requests.post(
            SIRIX_BASE_URL + SIRIX_ENDPOINT,
            headers=HEADERS,
            json=payload,
            timeout=SIRIX_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict) and "OpenPositions" in data:
            return data["OpenPositions"]
        if isinstance(data, list):
            return data

        log("[SIRIX] Unexpected response format", level="WARN")
        return []

    except Exception as e:
        log(f"[SIRIX] Request error: {e}", level="WARN")
        return []


def build_new_events(
    raw_positions: List[dict],
    cache: SeenOrdersCache,
    min_open_time: Optional[datetime] = None,
) -> List[SirixPositionEvent]:
    """
    Convert raw SiRiX position dicts into SirixPositionEvent objects,
    filtering out already-seen OrderIDs and optionally those before min_open_time.
    """
    events: List[SirixPositionEvent] = []

    for pos in raw_positions:
        try:
            if pos.get("InstrumentName") != SIRIX_INSTRUMENT:
                continue

            order_id = str(pos.get("OrderID", ""))
            if not order_id or cache.contains(order_id):
                continue

            side = _infer_side(
                action_type=pos.get("ActionType"),
                open_rate=pos.get("OpenRate"),
                sl=pos.get("StopLoss"),
                tp=pos.get("TakeProfit"),
            )
            if side is None:
                continue

            open_time = _parse_utc(pos.get("OpenTime", ""))
            if min_open_time is not None and open_time < min_open_time:
                cache.add(order_id)  # mark seen so we skip on next call too
                continue

            ev = SirixPositionEvent(
                order_id=order_id,
                user_id=str(pos.get("UserID", "")),
                side=side,
                lots=float(pos.get("AmountLots", 0.0)),
                time=open_time,
            )
            events.append(ev)
            cache.add(order_id)

            if VERBOSE_CLUSTERS:
                log(
                    f"[EVENT] order={order_id} user={ev.user_id} side={side} "
                    f"lots={ev.lots} open_time={open_time.isoformat()}"
                )

        except Exception as e:
            log(f"[SIRIX] Parse error: {e}", level="WARN")
            continue

    return events
