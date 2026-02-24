# src/main.py
"""
XAU SiRiX Cluster Hybrid Bot — entry point.

Run from project root:
    python -m src.main

The main loop is intentionally thin. All business logic lives in:
  · src/strategies/chandelier.py   — direction decision, entries, trailing, exits
  · src/mt5/execution.py           — order placement, position management
  · src/sirix/api.py               — SiRiX event fetching
  · src/core/risk.py               — daily loss limits
  · src/core/filters.py            — no-trade zones
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from typing import List, Optional

from pytz import timezone

# ── Bootstrap: logger and config must be imported first ──────────────────────
from config.config import (
    BOT_NAME, BOT_KEY, LOG_PATH, STATE_PATH,
    POLL_INTERVAL_SECONDS, EQUITY_HEARTBEAT_SECONDS,
)
from src.core.logger import init_logger, enable_print_capture, log, log_strategy
from src.core.models import StrategyState
from src.core.state import write_state
from src.core.filters import load_no_trade_zones, check_no_trade_zone
from src.core.risk import check_daily_loss_limits, floating_pnl, realized_pnl_today
from src.mt5 import connection as conn
from src.mt5.execution import refresh_and_log_closes, close_position
from src.sirix.api import SeenOrdersCache, fetch_raw_positions, build_new_events
from src.strategies.loader import load_strategies
from src.strategies.chandelier import (
    entry_step,
    manage_pending_orders,
    manage_trailing_stops,
    manage_time_exits,
)

import MetaTrader5 as mt5


def _utc_now() -> datetime:
    return datetime.now(timezone("UTC"))


# ─────────────────────────────────────────────
# FLATTEN ALL EXPOSURE  (no-trade / daily-limit)
# ─────────────────────────────────────────────

def flatten_all(strategies: List[StrategyState], reason: str) -> None:
    """
    Close all open positions and cancel all pending orders across all engines.
    Also clears cluster buffers so we only act on fresh signals after resuming.
    """
    log(f"[FLATTEN] {reason}", level="WARN")

    for st in strategies:
        cfg = st.config

        # Cancel pending orders
        for ticket in list(st.pending_orders.keys()):
            from config.config import make_comment
            req = {
                "action":  mt5.TRADE_ACTION_REMOVE,
                "order":   ticket,
                "symbol":  conn.MT5_SYMBOL if hasattr(conn, "MT5_SYMBOL") else "XAUUSD",
                "magic":   cfg.magic,
                "comment": make_comment(f"{cfg.name}_FLT"),
            }
            res = mt5.order_send(req)
            if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
                log_strategy(cfg, f"[WARN] cancel pending failed ticket={ticket}", level="WARN")
            st.pending_orders.pop(ticket, None)
            st.pending_meta.pop(ticket, None)

        # Close open positions
        for ticket in list(st.open_positions.keys()):
            close_position(ticket, cfg, reason=reason)
            st.open_positions.pop(ticket, None)

        # Clear cluster engine so only NEW events trigger entries after resume
        st.cluster_engine.events.clear()
        st.cluster_engine.last_cluster_time = None
        st.cluster_engine.last_cluster_side = None


# ─────────────────────────────────────────────
# EQUITY HEARTBEAT
# ─────────────────────────────────────────────

def _maybe_log_equity(strategies: List[StrategyState], now: datetime) -> None:
    """Log account equity + per-engine PnL every EQUITY_HEARTBEAT_SECONDS."""
    if EQUITY_HEARTBEAT_SECONDS <= 0:
        return

    for st in strategies:
        last = st._last_equity_heartbeat_utc
        if last is None or (now - last).total_seconds() >= EQUITY_HEARTBEAT_SECONDS:
            acct = mt5.account_info()
            if acct is None:
                continue
            rpnl = realized_pnl_today(st.config.magic)
            fpnl = floating_pnl(st.config.magic)
            log_strategy(
                st.config,
                f"[HEARTBEAT] equity={acct.equity:.2f} "
                f"realized_today={rpnl:+.2f} floating={fpnl:+.2f}",
                equity=acct.equity,
                realized_today=rpnl,
                floating=fpnl,
            )
            st._last_equity_heartbeat_utc = now


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

def run_loop(strategies: List[StrategyState]) -> None:
    from config.config import MT5_SYMBOL

    # No-trade zone state (local to this function)
    ntz_active:     bool               = False
    ntz_reason:     Optional[str]      = None
    ntz_entered_at: Optional[datetime] = None

    # ── Sirix lookback window ────────────────────────────────────────────
    max_t = max(st.config.t_seconds for st in strategies)
    lookback_seconds = max(max_t, 60)

    # ── Bootstrap: mark all current SiRiX orders as already-seen ─────────
    seen = SeenOrdersCache()
    bootstrap_raw = fetch_raw_positions(lookback_seconds)
    bootstrap_ids = {
        str(p.get("OrderID", ""))
        for p in bootstrap_raw
        if p.get("InstrumentName") == "XAUUSD" and p.get("OrderID")
    }
    seen.bootstrap(bootstrap_ids)

    log(f"=== {BOT_NAME} starting — {len(strategies)} strategy engine(s) ===")
    for st in strategies:
        cfg = st.config
        log(
            f"[ENGINE] {cfg.name} | MAGIC={cfg.magic} | "
            f"T={cfg.t_seconds}s K={cfg.k_unique} | "
            f"stop={cfg.stop_mode} chan_lb={cfg.chan_lookback} | "
            f"direction={cfg.direction_mode} | "
            f"risk={cfg.risk_percent*100:.1f}% | "
            f"offset=${cfg.limit_offset_dollars:.2f}"
        )

    # ── Main loop ─────────────────────────────────────────────────────────
    while True:
        try:
            now = _utc_now()

            # ── 0a: MT5 health check ─────────────────────────────────────
            if not conn.ensure_connected():
                log("[MAIN] MT5 reconnect failed — sleeping 30s", level="ERROR")
                time.sleep(30)
                continue

            # ── 0b: Sync positions + manage pending TTL ──────────────────
            for st in strategies:
                refresh_and_log_closes(st)
                manage_pending_orders(st)

            # ── 0c: Equity heartbeat ─────────────────────────────────────
            _maybe_log_equity(strategies, now)

            # ── 1: No-trade zone check ────────────────────────────────────
            zones    = load_no_trade_zones()
            in_zone, zone_reason = check_no_trade_zone(now, zones)

            if in_zone:
                if not ntz_active:
                    ntz_active     = True
                    ntz_reason     = zone_reason
                    ntz_entered_at = now
                    log(f"[NTZ] ENTER zone: {ntz_reason} — flattening all exposure", level="WARN")
                    flatten_all(strategies, reason=ntz_reason or "NoTradeZone")

                write_state(strategies)
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            if ntz_active:
                log(f"[NTZ] EXIT zone: {ntz_reason} (was active since {ntz_entered_at})")
                ntz_active = False
                ntz_reason = None
                ntz_entered_at = None

            # ── 2: Daily loss circuit breaker ─────────────────────────────
            breach, breach_reason = check_daily_loss_limits(strategies)
            if breach:
                log(f"[RISK] DAILY LOSS BREACH: {breach_reason}", level="ERROR")
                flatten_all(strategies, reason=breach_reason)
                write_state(strategies)
                log("[RISK] Bot stopped. Manual restart required.", level="ERROR")
                break

            # ── 3: Fetch fresh SiRiX events ───────────────────────────────
            raw       = fetch_raw_positions(lookback_seconds)
            new_events = build_new_events(raw, seen)

            # ── 4: Entry logic per strategy ───────────────────────────────
            for st in strategies:
                entry_step(st, new_events, now)

            # ── 5: Position management per strategy ───────────────────────
            for st in strategies:
                manage_trailing_stops(st)
                manage_time_exits(st)

            # ── 6: State snapshot ─────────────────────────────────────────
            write_state(strategies)

            time.sleep(POLL_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            log("\n[MAIN] KeyboardInterrupt — stopping bot.")
            break
        except Exception as e:
            log(f"[MAIN] Unhandled exception: {type(e).__name__}: {e}", level="ERROR")
            time.sleep(2)   # brief pause before retrying


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main() -> None:
    # 1) Init logger first (so all subsequent log() calls work)
    init_logger(BOT_NAME, LOG_PATH)
    enable_print_capture()

    log(f"Log file : {LOG_PATH.resolve()}")
    log(f"State file: {STATE_PATH.resolve()}")

    # 2) Connect to MT5
    conn.init_mt5()

    # 3) Load strategies from YAML
    strategies = load_strategies(BOT_KEY)

    # 4) Run
    try:
        run_loop(strategies)
    finally:
        mt5.shutdown()
        log("[MT5] Shutdown complete.")


if __name__ == "__main__":
    main()
