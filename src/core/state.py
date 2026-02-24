# src/core/state.py
"""
Bot state persistence.
Writes a human-readable JSON snapshot every loop for external monitoring
(dashboards, watchdog scripts, etc.).
"""
from __future__ import annotations

import json
from typing import List, TYPE_CHECKING

from config.config import BOT_NAME, STATE_PATH
from src.core.logger import log

if TYPE_CHECKING:
    from src.core.models import StrategyState


def write_state(strategies: "List[StrategyState]") -> None:
    """
    Dump a JSON snapshot of all strategy states to STATE_PATH.
    Never raises — logging failures must not crash the bot.
    """
    try:
        from pytz import timezone
        from datetime import datetime

        def utc_now():
            return datetime.now(timezone("UTC"))

        payload = {
            "bot_name":   BOT_NAME,
            "updated_utc": utc_now().isoformat(),
            "strategies": [],
        }

        for st in strategies:
            cfg = st.config
            ce  = st.cluster_engine

            events_list = list(ce.events)
            buy_users   = {ev.user_id for ev in events_list if ev.side == "buy"}
            sell_users  = {ev.user_id for ev in events_list if ev.side == "sell"}
            last_event  = max((ev.time for ev in events_list), default=None)

            open_pos_list = [
                {
                    "ticket":           info.ticket,
                    "direction":        info.direction,
                    "trade_mode":       info.trade_mode,
                    "entry_time_utc":   info.entry_time.isoformat(),
                    "entry_price":      float(info.entry_price),
                    "sl_price":         float(info.sl_price),
                    "initial_sl_price": float(info.initial_sl_price),
                    "tp_price":         float(info.tp_price) if info.tp_price else None,
                    "breakeven_hit":    info.breakeven_hit,
                }
                for info in st.open_positions.values()
            ]

            recent_events = [
                {
                    "order_id": ev.order_id,
                    "user_id":  ev.user_id,
                    "side":     ev.side,
                    "lots":     ev.lots,
                    "time_utc": ev.time.isoformat(),
                }
                for ev in events_list[-10:]
            ]

            payload["strategies"].append({
                "name":  cfg.name,
                "magic": cfg.magic,
                "config": {
                    "t_seconds":       cfg.t_seconds,
                    "k_unique":        cfg.k_unique,
                    "stop_mode":       cfg.stop_mode,
                    "direction_mode":  cfg.direction_mode,
                    "risk_mode":       cfg.risk_mode,
                    "risk_percent":    cfg.risk_percent,
                    "trail_start_R":   cfg.trail_start_R,
                    "breakeven_R":     cfg.breakeven_trigger_R,
                },
                "cluster": {
                    "window_seconds":          ce.window_seconds,
                    "events_in_window":        len(events_list),
                    "unique_buy":              len(buy_users),
                    "unique_sell":             len(sell_users),
                    "last_cluster_side":       ce.last_cluster_side,
                    "last_cluster_time_utc":   ce.last_cluster_time.isoformat() if ce.last_cluster_time else None,
                    "last_event_time_utc":     last_event.isoformat() if last_event else None,
                    "recent_events":           recent_events,
                },
                "open_positions":  open_pos_list,
                "pending_orders":  list(st.pending_orders.keys()),
                "cooldown_until":  st.cooldown_until_utc.isoformat() if st.cooldown_until_utc else None,
            })

        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with STATE_PATH.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    except Exception as e:
        log(f"[STATE] Failed to write state: {e}", level="WARN")
