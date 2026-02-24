# src/core/models.py
"""
All shared dataclasses for the bot.
No business logic here — pure data containers only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.cluster_engine import ClusterEngine


# ─────────────────────────────────────────────
# SIRIX EVENT
# ─────────────────────────────────────────────

@dataclass
class SirixPositionEvent:
    """One open position observed on SiRiX (de-duplicated by OrderID)."""
    order_id: str
    user_id:  str
    side:     str      # "buy" | "sell"
    lots:     float
    time:     datetime  # UTC-aware OpenTime from SiRiX


# ─────────────────────────────────────────────
# MT5 POSITION TRACKING
# ─────────────────────────────────────────────

@dataclass
class BotPositionInfo:
    """Local snapshot of one open MT5 position managed by this bot."""
    ticket:           int
    direction:        str            # "buy" | "sell"
    entry_time:       datetime       # UTC
    entry_price:      float
    sl_price:         float          # current (may have moved with trail)
    tp_price:         Optional[float]
    initial_sl_price: float          # original SL at entry (never changes)
    trade_mode:       str = "inverse"  # "inverse" | "momentum"
    breakeven_hit:    bool = False   # True once we have moved SL to entry


@dataclass
class PendingOrderMeta:
    """Metadata captured at pending order placement (for fill quality logging)."""
    created_at_utc: datetime
    trade_side:     str      # "buy" | "sell"
    pending_price:  float    # the LIMIT price we requested
    market_price:   float    # mid-price at placement time
    trade_mode:     str = "inverse"


# ─────────────────────────────────────────────
# STRATEGY CONFIGURATION
# ─────────────────────────────────────────────

@dataclass
class StrategyConfig:
    """
    Full parameter set for one strategy engine.
    Populated from strategies.yaml by src/strategies/loader.py.
    """

    # Identity
    name:  str
    magic: int

    # Cluster detection
    t_seconds: int
    k_unique:  int

    # Exits
    hold_minutes:   int
    sl_distance:    float
    tp_R_multiple:  float
    use_tp_exit:    bool
    use_time_exit:  bool

    # Stop mode
    stop_mode:      str            # fixed | atr_static | atr_trailing | chandelier
    atr_period:     int
    atr_init_mult:  float
    atr_trail_mult: float
    chan_lookback:  Optional[int]

    # Trailing gate
    trail_start_R:  Optional[float]

    # Breakeven (move SL to entry at +be_trigger_R)
    breakeven_trigger_R: Optional[float]

    # Entry
    limit_offset_dollars: float
    max_open_positions:   int

    # Risk
    risk_mode:               str    # dynamic_pct | static_pct | fixed_lots
    risk_percent:            float
    fixed_lots:              float
    static_risk_base_balance: float

    # Direction
    direction_mode:     str   = "hybrid"   # inverse | momentum | hybrid
    rsi_period:         int   = 14
    rsi_overbought:     float = 65.0
    rsi_oversold:       float = 35.0
    vwap_band_pct:      float = 0.001
    hybrid_require_both: bool = True


# ─────────────────────────────────────────────
# STRATEGY RUNTIME STATE
# ─────────────────────────────────────────────

@dataclass
class StrategyState:
    """
    All mutable runtime state for one strategy engine.
    One StrategyState exists per entry in strategies.yaml.
    """
    config:          StrategyConfig
    cluster_engine:  "ClusterEngine"

    open_positions:  Dict[int, BotPositionInfo]  = field(default_factory=dict)
    pending_orders:  Dict[int, datetime]          = field(default_factory=dict)  # ticket → created_at
    pending_meta:    Dict[int, PendingOrderMeta]  = field(default_factory=dict)

    cooldown_until_utc:          Optional[datetime] = None

    # Internal: used by log_cooldown_state to avoid spam
    _cooldown_active_last:       bool               = False
    _cooldown_last_heartbeat_utc: Optional[datetime] = None

    # Internal: equity heartbeat tracking
    _last_equity_heartbeat_utc:  Optional[datetime] = None
