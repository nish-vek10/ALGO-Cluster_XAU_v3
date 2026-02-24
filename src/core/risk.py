# src/core/risk.py
"""
Risk management:
  1. Lot-size calculation (dynamic_pct / static_pct / fixed_lots).
  2. Daily PnL aggregation per magic number.
  3. Daily loss limit circuit breaker (total + per-engine).
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Tuple, TYPE_CHECKING

import MetaTrader5 as mt5

from config.config import (
    USE_DAILY_LOSS_LIMITS,
    DAILY_LOSS_LIMIT_TOTAL,
    DAILY_LOSS_LIMIT_PER_ENGINE,
    LOCAL_TZ, MT5_SYMBOL,
)
from src.core.logger import log

if TYPE_CHECKING:
    from src.core.models import StrategyConfig, StrategyState


# ─────────────────────────────────────────────
# LOT SIZING
# ─────────────────────────────────────────────

def calc_lot_size(
    stop_distance_price: float,
    cfg: "StrategyConfig",
    symbol_info,          # mt5.symbol_info object (passed in to avoid global)
) -> float:
    """
    Compute position size in lots so that a full SL hit costs exactly
    `risk_dollars` (or use fixed_lots if risk_mode = "fixed_lots").

    Modes:
      fixed_lots   → always use cfg.fixed_lots
      dynamic_pct  → risk_dollars = current_equity × cfg.risk_percent
      static_pct   → risk_dollars = cfg.static_risk_base_balance × cfg.risk_percent
    """
    if cfg.risk_mode == "fixed_lots":
        return _round_lots(cfg.fixed_lots, symbol_info)

    # Determine dollar risk
    if cfg.risk_mode == "static_pct":
        risk_dollars = cfg.static_risk_base_balance * cfg.risk_percent
    else:
        # dynamic_pct — use current equity
        acct = mt5.account_info()
        if acct is None:
            log("[RISK] account_info() is None — defaulting to vol_min", level="WARN")
            return _round_lots(symbol_info.volume_min, symbol_info)
        risk_dollars = acct.equity * cfg.risk_percent

    contract_size = symbol_info.trade_contract_size
    if stop_distance_price <= 0.0 or contract_size <= 0.0:
        return _round_lots(symbol_info.volume_min, symbol_info)

    raw_lots = risk_dollars / (stop_distance_price * contract_size)
    return _round_lots(raw_lots, symbol_info)


def _round_lots(lots: float, symbol_info) -> float:
    """Snap lots to broker volume grid [vol_min, vol_max] in steps of volume_step."""
    step    = max(symbol_info.volume_step, 0.01)
    vol_min = symbol_info.volume_min
    vol_max = symbol_info.volume_max
    rounded = round(round(lots / step) * step, 2)
    return max(vol_min, min(rounded, vol_max))


# ─────────────────────────────────────────────
# DAILY PNL HELPERS
# ─────────────────────────────────────────────

def _start_of_local_day_utc(now_utc: datetime) -> datetime:
    """Return UTC equivalent of today's midnight in LOCAL_TZ."""
    local = now_utc.astimezone(LOCAL_TZ)
    local_midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    from pytz import timezone
    return local_midnight.astimezone(timezone("UTC"))


def realized_pnl_today(magic: int) -> float:
    """
    Sum of closed-deal profit for this magic since today's local midnight.
    Uses MT5 deal history (DEAL_ENTRY_OUT / OUT_BY only → closing deals).
    """
    now   = datetime.now(__import__("pytz").timezone("UTC"))
    start = _start_of_local_day_utc(now)
    deals = mt5.history_deals_get(start, now)
    if deals is None:
        return 0.0

    return sum(
        float(getattr(d, "profit", 0.0))
        for d in deals
        if getattr(d, "magic",   None) == magic
        and getattr(d, "entry",  None) in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)
    )


def floating_pnl(magic: int) -> float:
    """Sum of unrealised profit across all open positions for this magic."""
    poss = mt5.positions_get(symbol=MT5_SYMBOL)
    if not poss:
        return 0.0
    return sum(
        float(getattr(p, "profit", 0.0))
        for p in poss
        if p.magic == magic
    )


# ─────────────────────────────────────────────
# DAILY LOSS CIRCUIT BREAKER
# ─────────────────────────────────────────────

def check_daily_loss_limits(
    strategies: "List[StrategyState]",
) -> Tuple[bool, str]:
    """
    Returns (breach: bool, reason: str).

    Checks:
      1. Any single engine: realized + floating <= -DAILY_LOSS_LIMIT_PER_ENGINE
      2. All engines combined: total <= -DAILY_LOSS_LIMIT_TOTAL

    Called every loop iteration BEFORE placing new orders.
    """
    if not USE_DAILY_LOSS_LIMITS:
        return False, ""

    total_pnl = 0.0

    for st in strategies:
        magic = st.config.magic
        pnl   = realized_pnl_today(magic) + floating_pnl(magic)
        total_pnl += pnl

        if pnl <= -DAILY_LOSS_LIMIT_PER_ENGINE:
            return (
                True,
                f"ENGINE_DAILY_LOSS_LIMIT: {st.config.name} pnl_today={pnl:.2f}",
            )

    if total_pnl <= -DAILY_LOSS_LIMIT_TOTAL:
        return (
            True,
            f"TOTAL_DAILY_LOSS_LIMIT: total_pnl_today={total_pnl:.2f}",
        )

    return False, ""
