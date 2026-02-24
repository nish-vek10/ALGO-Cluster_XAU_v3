# src/strategies/loader.py
"""
Strategy loader.

Reads config/strategies.yaml, validates required fields, and constructs
a list of StrategyState objects ready for the main loop.

To add a new bot:
  1. Add a new top-level key in strategies.yaml (e.g. xau_second_bot).
  2. Create a new main script that calls load_strategies("xau_second_bot").
  3. The new script uses the same core infrastructure; only the strategy
     params and direction logic differ.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

import yaml

from config.config import STRATEGIES_YAML_PATH
from src.core.models import StrategyConfig, StrategyState
from src.core.cluster_engine import ClusterEngine
from src.core.logger import log


# Required YAML fields per strategy entry.
# Missing any of these will raise a clear error at startup.
_REQUIRED = [
    "name", "magic",
    "t_seconds", "k_unique",
    "hold_minutes", "sl_distance", "tp_R_multiple", "use_tp_exit", "use_time_exit",
    "stop_mode", "atr_period", "atr_init_mult", "atr_trail_mult",
    "limit_offset_dollars", "max_open_positions",
    "risk_mode", "risk_percent", "fixed_lots", "static_risk_base_balance",
]


def load_strategies(bot_key: str) -> List[StrategyState]:
    """
    Load and validate strategy configs from strategies.yaml for the given bot_key.

    Returns a list of StrategyState objects, one per enabled strategy entry.
    """
    if not STRATEGIES_YAML_PATH.exists():
        raise FileNotFoundError(f"strategies.yaml not found at {STRATEGIES_YAML_PATH}")

    with STRATEGIES_YAML_PATH.open("r", encoding="utf-8") as f:
        all_data = yaml.safe_load(f)

    if bot_key not in all_data:
        raise KeyError(
            f"Bot key '{bot_key}' not found in strategies.yaml. "
            f"Available keys: {list(all_data.keys())}"
        )

    raw_list = all_data[bot_key]
    if not isinstance(raw_list, list):
        raise TypeError(f"Expected a list under key '{bot_key}' in strategies.yaml")

    states: List[StrategyState] = []

    for entry in raw_list:
        # Skip disabled strategies
        if not entry.get("enabled", True):
            log(f"[LOADER] Skipping disabled strategy: {entry.get('name', '?')}")
            continue

        # Validate required fields
        missing = [k for k in _REQUIRED if k not in entry]
        if missing:
            raise ValueError(
                f"Strategy '{entry.get('name','?')}' is missing required fields: {missing}"
            )

        cfg = StrategyConfig(
            name=entry["name"],
            magic=int(entry["magic"]),

            t_seconds=int(entry["t_seconds"]),
            k_unique=int(entry["k_unique"]),

            hold_minutes=int(entry["hold_minutes"]),
            sl_distance=float(entry["sl_distance"]),
            tp_R_multiple=float(entry["tp_R_multiple"]),
            use_tp_exit=bool(entry["use_tp_exit"]),
            use_time_exit=bool(entry["use_time_exit"]),

            stop_mode=str(entry["stop_mode"]),
            atr_period=int(entry["atr_period"]),
            atr_init_mult=float(entry["atr_init_mult"]),
            atr_trail_mult=float(entry["atr_trail_mult"]),
            chan_lookback=entry.get("chan_lookback"),  # Optional[int]

            trail_start_R=entry.get("trail_start_R"),            # Optional[float]
            breakeven_trigger_R=entry.get("breakeven_trigger_R"), # Optional[float]

            limit_offset_dollars=float(entry["limit_offset_dollars"]),
            max_open_positions=int(entry["max_open_positions"]),

            risk_mode=str(entry["risk_mode"]),
            risk_percent=float(entry["risk_percent"]),
            fixed_lots=float(entry["fixed_lots"]),
            static_risk_base_balance=float(entry["static_risk_base_balance"]),

            # Hybrid / direction
            direction_mode=str(entry.get("direction_mode", "hybrid")),
            rsi_period=int(entry.get("rsi_period", 14)),
            rsi_overbought=float(entry.get("rsi_overbought", 65.0)),
            rsi_oversold=float(entry.get("rsi_oversold", 35.0)),
            vwap_band_pct=float(entry.get("vwap_band_pct", 0.001)),
            hybrid_require_both=bool(entry.get("hybrid_require_both", True)),
        )

        cluster_engine = ClusterEngine(
            window_seconds=cfg.t_seconds,
            k_unique=cfg.k_unique,
        )

        states.append(StrategyState(config=cfg, cluster_engine=cluster_engine))
        log(
            f"[LOADER] Loaded strategy: {cfg.name} | magic={cfg.magic} | "
            f"T={cfg.t_seconds}s K={cfg.k_unique} | stop={cfg.stop_mode} | "
            f"direction={cfg.direction_mode} | risk={cfg.risk_percent*100:.1f}%"
        )

    if not states:
        raise RuntimeError(f"No enabled strategies found under bot_key='{bot_key}'")

    return states
