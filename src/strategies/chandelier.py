# src/strategies/chandelier.py
"""
Chandelier strategy — per-loop step, trailing stops, exits, and hybrid direction logic.

HYBRID DIRECTION MODEL
───────────────────────────────────────────────────────────────────────────────
The original bot always fades the cluster (INVERSE). This sometimes works
against us when the cluster is actually right and riding real momentum.

We now add a MOMENTUM path that goes WITH the cluster when RSI and VWAP
together confirm that the crowd is following a genuine move:

  SELL cluster → go WITH (sell) if:
    · RSI > rsi_overbought   (price is overbought → real selling pressure)
    · price is above VWAP by at least vwap_band_pct   (bullish day bias broken)

  BUY cluster → go WITH (buy) if:
    · RSI < rsi_oversold     (price is oversold → real buying / bounce)
    · price is below VWAP by at least vwap_band_pct   (bearish day bias broken)

  Otherwise → INVERSE (fade the crowd as before)

Why this combination?
  RSI alone catches extreme momentum but fires too often on choppy days.
  VWAP alone can be too slow on fast-moving sessions.
  Together they filter out the noise: we only follow the crowd when momentum
  is both statistically extreme (RSI) AND the daily anchor price (VWAP)
  has been meaningfully left behind.

ENTRY ORDER (both modes)
───────────────────────────────────────────────────────────────────────────────
  BUY  entry → BUY_LIMIT  @ bid − offset   (improves fill vs chasing market)
  SELL entry → SELL_LIMIT @ ask + offset   (improves fill vs chasing market)

  This applies identically for inverse and momentum entries, ensuring we
  always get a slightly better price than the current mid regardless of direction.

BREAKEVEN LOGIC
───────────────────────────────────────────────────────────────────────────────
  Once the position reaches +breakeven_trigger_R profit, SL is moved to
  entry price (breakeven). This runs before the chandelier trail check,
  so the trail never moves SL back below entry once breakeven is hit.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, List, TYPE_CHECKING, Tuple

import MetaTrader5 as mt5

from config.config import (
    MT5_SYMBOL, TRADE_COOLDOWN_SECONDS,
    VERBOSE_HYBRID, VERBOSE_CLUSTER_DEBUG,
)
from src.core.models import SirixPositionEvent
from src.core.indicators import fetch_m1_rates, compute_atr, compute_rsi, compute_vwap
from src.core.logger import log_strategy
from src.core.filters import within_session
from src.mt5.execution import (
    place_pending_entry, close_position, modify_sl_tp,
    enforce_stop_level, fmt_price,
)
import src.mt5.connection as conn

if TYPE_CHECKING:
    from src.core.models import StrategyConfig, StrategyState


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _utc_now() -> datetime:
    from pytz import timezone
    return datetime.now(timezone("UTC"))


def _inverse_side(side: str) -> str:
    return "sell" if side == "buy" else "buy"


def _in_cooldown(state: "StrategyState", now: datetime) -> bool:
    return state.cooldown_until_utc is not None and now < state.cooldown_until_utc


def _log_cooldown(state: "StrategyState", now: datetime) -> bool:
    """Log cooldown start/end once. Returns whether cooldown is currently active."""
    from config.config import COOLDOWN_HEARTBEAT_SECONDS
    active = _in_cooldown(state, now)
    cfg    = state.config

    if active and not state._cooldown_active_last:
        log_strategy(cfg, f"[COOLDOWN] START until {state.cooldown_until_utc.isoformat()}")
        state._cooldown_last_heartbeat_utc = now

    if not active and state._cooldown_active_last:
        log_strategy(cfg, "[COOLDOWN] END")
        state._cooldown_last_heartbeat_utc = None

    if active and COOLDOWN_HEARTBEAT_SECONDS > 0:
        last_hb = state._cooldown_last_heartbeat_utc
        if last_hb is None or (now - last_hb).total_seconds() >= COOLDOWN_HEARTBEAT_SECONDS:
            remaining = max(0, int((state.cooldown_until_utc - now).total_seconds()))
            log_strategy(cfg, f"[COOLDOWN] remaining={remaining}s")
            state._cooldown_last_heartbeat_utc = now

    state._cooldown_active_last = active
    return active


# ─────────────────────────────────────────────
# HYBRID DIRECTION DECISION
# ─────────────────────────────────────────────

def decide_direction(
    cluster_side: str,
    cfg: "StrategyConfig",
) -> Tuple[str, str]:
    """
    Determine trade direction and mode for a detected cluster.

    Returns:
      (trade_side, mode)
      trade_side : "buy" | "sell"
      mode       : "inverse" | "momentum"

    Logic:
      inverse  → always fade
      momentum → always follow
      hybrid   → use RSI + VWAP to decide per cluster
    """
    if cfg.direction_mode == "inverse":
        return _inverse_side(cluster_side), "inverse"

    if cfg.direction_mode == "momentum":
        return cluster_side, "momentum"

    # ── Hybrid ─────────────────────────────────────────────────────────────
    # Fetch indicators (M1 bars — shared fetch, used for both RSI and VWAP)
    try:
        bars_needed = max(cfg.rsi_period + 5, 300)   # 300 bars ≈ 5 hrs of M1
        df          = fetch_m1_rates(MT5_SYMBOL, bars=bars_needed)
        rsi         = compute_rsi(df, cfg.rsi_period)
        vwap        = compute_vwap(df)
        current_px  = float(df["close"].iloc[-1])
    except Exception as e:
        # If indicator fetch fails, fall back to inverse (safer)
        log_strategy(cfg, f"[HYBRID] Indicator fetch failed ({e}) — defaulting to inverse", level="WARN")
        return _inverse_side(cluster_side), "inverse"

    # ── Evaluate momentum conditions ──────────────────────────────────────
    if cluster_side == "sell":
        # Crowd is selling — go WITH if:
        #   RSI overbought  (momentum is bearish)
        #   price clearly above VWAP  (price ran up, now rolling over)
        rsi_cond  = rsi > cfg.rsi_overbought
        vwap_cond = current_px > vwap * (1.0 + cfg.vwap_band_pct)
    else:
        # Crowd is buying — go WITH if:
        #   RSI oversold  (momentum is bullish)
        #   price clearly below VWAP  (price sold off, now bouncing)
        rsi_cond  = rsi < cfg.rsi_oversold
        vwap_cond = current_px < vwap * (1.0 - cfg.vwap_band_pct)

    if cfg.hybrid_require_both:
        momentum = rsi_cond and vwap_cond
    else:
        momentum = rsi_cond or vwap_cond

    mode       = "momentum" if momentum else "inverse"
    trade_side = cluster_side if momentum else _inverse_side(cluster_side)

    if VERBOSE_HYBRID:
        log_strategy(
            cfg,
            f"[HYBRID] cluster={cluster_side} RSI={rsi:.1f} VWAP={fmt_price(vwap)} "
            f"price={fmt_price(current_px)} "
            f"rsi_cond={rsi_cond} vwap_cond={vwap_cond} "
            f"→ mode={mode} trade_side={trade_side}",
            cluster_side=cluster_side,
            rsi=round(rsi, 2),
            vwap=round(vwap, 2),
            price=round(current_px, 2),
            rsi_cond=rsi_cond,
            vwap_cond=vwap_cond,
            mode=mode,
            trade_side=trade_side,
        )

    return trade_side, mode


# ─────────────────────────────────────────────
# ENTRY STEP  (called from main loop per strategy)
# ─────────────────────────────────────────────

def entry_step(
    state: "StrategyState",
    new_events: List[SirixPositionEvent],
    now: datetime,
) -> None:
    """
    Feed new SiRiX events into the cluster engine and, if a cluster fires
    and all gates pass, place a pending limit order.
    """
    cfg = state.config

    # Gate 1: already at max capacity
    total_open = len(state.open_positions) + len(state.pending_orders)
    if total_open >= cfg.max_open_positions:
        return

    # Gate 2: only 1 pending order allowed at a time per engine
    if len(state.pending_orders) > 0:
        return

    # Gate 3: cooldown (anti-spam after recent fill or placement)
    if _log_cooldown(state, now):
        return

    # Gate 4: session filter
    if not within_session():
        return

    # Gate 5: cluster detection
    cluster_side = state.cluster_engine.add_events(new_events)
    if cluster_side is None:
        return

    # Gate 6: decide direction (hybrid logic)
    trade_side, trade_mode = decide_direction(cluster_side, cfg)

    # Gate 7: place pending limit entry
    result = place_pending_entry(
        trade_side=trade_side,
        cfg=cfg,
        offset_dollars=cfg.limit_offset_dollars,
        trade_mode=trade_mode,
    )

    if result is not None:
        ticket, meta = result
        state.pending_orders[ticket] = meta.created_at_utc
        state.pending_meta[ticket]   = meta
        # Start cooldown immediately after placement
        state.cooldown_until_utc = now + timedelta(seconds=TRADE_COOLDOWN_SECONDS)


# ─────────────────────────────────────────────
# PENDING ORDER MANAGEMENT
# ─────────────────────────────────────────────

def manage_pending_orders(state: "StrategyState") -> None:
    """
    1. Remove internal records for orders that are no longer active in MT5.
    2. Cancel any pending order that has exceeded PENDING_ORDER_TIMEOUT_MIN.
    """
    from config.config import PENDING_ORDER_TIMEOUT_MIN
    from config.config import make_comment

    cfg = state.config
    if not state.pending_orders:
        return

    now = _utc_now()

    # Snapshot of active pending orders for this magic
    mt5_orders = mt5.orders_get(symbol=MT5_SYMBOL)
    active: dict[int, object] = {}
    if mt5_orders:
        for o in mt5_orders:
            if o.magic == cfg.magic:
                active[o.ticket] = o

    # Pass 1: drop records for orders no longer in MT5 (filled or externally cancelled)
    for ticket in list(state.pending_orders.keys()):
        if ticket not in active:
            state.pending_orders.pop(ticket, None)
            state.pending_meta.pop(ticket, None)

    if not state.pending_orders:
        return

    # Pass 2: cancel orders that have exceeded the TTL
    for ticket, created_at in list(state.pending_orders.items()):
        age_min = (now - created_at).total_seconds() / 60.0
        if age_min < PENDING_ORDER_TIMEOUT_MIN:
            continue

        req = {
            "action":  mt5.TRADE_ACTION_REMOVE,
            "order":   ticket,
            "symbol":  MT5_SYMBOL,
            "magic":   cfg.magic,
            "comment": make_comment(f"{cfg.name}_TTL"),
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            log_strategy(
                cfg,
                f"[WARN] cancel pending failed ticket={ticket} "
                f"retcode={getattr(res,'retcode',None)}",
                level="WARN", ticket=ticket,
            )
        else:
            log_strategy(cfg, f"[CANCEL] Pending TTL expired ticket={ticket} age={age_min:.1f}min")

        state.pending_orders.pop(ticket, None)
        state.pending_meta.pop(ticket, None)


# ─────────────────────────────────────────────
# TRAILING STOPS  (chandelier + breakeven)
# ─────────────────────────────────────────────

def manage_trailing_stops(state: "StrategyState") -> None:
    """
    On each M1 bar, update SL for all open positions using the chandelier method.

    Order of operations per position:
      1. Skip if still on entry bar (give trade room to breathe).
      2. Compute open_R to check trail_start_R gate.
      3. Apply breakeven (move SL to entry at +breakeven_trigger_R) — done first,
         so trailing never moves SL back below entry.
      4. Apply chandelier trailing: SL = HH(lookback) − ATR × atr_trail_mult
         (for buys) or LL(lookback) + ATR × atr_trail_mult (for sells).
      5. Enforce broker stop level and safety guards.
      6. Send modify only if SL moved by more than 2 points.
    """
    cfg = state.config
    if cfg.stop_mode != "chandelier" or not state.open_positions:
        return

    # ── Fetch candles once, shared by all positions ────────────────────────
    try:
        lookback = cfg.chan_lookback or 30
        bars     = max(cfg.atr_period + 20, lookback + 5)
        df       = fetch_m1_rates(MT5_SYMBOL, bars=bars)
    except Exception as e:
        log_strategy(cfg, f"[TRAIL] Failed to fetch M1 rates: {e}", level="WARN")
        return

    atr_val      = compute_atr(df, cfg.atr_period)
    highest_high = float(df["high"].iloc[-lookback:].max())
    lowest_low   = float(df["low"].iloc[-lookback:].min())
    last_bar_ts  = df["time"].iloc[-1]
    current_px   = float(df["close"].iloc[-1])
    point        = conn.SYMBOL_INFO.point

    for ticket, info in list(state.open_positions.items()):
        # Verify position still exists in MT5
        poss = mt5.positions_get()
        pos  = None
        if poss:
            for p in poss:
                if p.ticket == ticket and p.magic == cfg.magic:
                    pos = p
                    break
        if pos is None:
            state.open_positions.pop(ticket, None)
            continue

        # 1) Don't trail on entry bar
        if info.entry_time >= last_bar_ts:
            continue

        # 2) Compute open_R for gates
        sl_dist = abs(info.entry_price - info.initial_sl_price)
        if sl_dist <= 0:
            continue

        if info.direction == "buy":
            open_R = (current_px - info.entry_price) / sl_dist
        else:
            open_R = (info.entry_price - current_px) / sl_dist

        # 3) Breakeven: move SL to entry once we hit +breakeven_trigger_R
        if (
            cfg.breakeven_trigger_R is not None
            and not info.breakeven_hit
            and open_R >= cfg.breakeven_trigger_R
        ):
            be_sl = round(info.entry_price, conn.SYMBOL_INFO.digits)
            # Only move SL if it improves (moves in our favour)
            improved = (
                (info.direction == "buy"  and be_sl > info.sl_price)
                or (info.direction == "sell" and be_sl < info.sl_price)
            )
            if improved:
                modify_sl_tp(ticket, cfg, be_sl, info.tp_price)
                info.sl_price    = be_sl
                info.breakeven_hit = True
                log_strategy(
                    cfg,
                    f"[BREAKEVEN] ticket={ticket} SL moved to entry={fmt_price(be_sl)} "
                    f"open_R={open_R:.2f}",
                    ticket=ticket, open_R=round(open_R, 3),
                )

        # 4) Trailing gate: only trail if open_R >= trail_start_R
        if cfg.trail_start_R is not None and open_R < cfg.trail_start_R:
            continue

        # 5) Chandelier candidate SL
        if info.direction == "buy":
            cand_sl = highest_high - cfg.atr_trail_mult * atr_val
            new_sl  = max(info.sl_price, cand_sl)   # never move SL backwards
        else:
            cand_sl = lowest_low + cfg.atr_trail_mult * atr_val
            new_sl  = min(info.sl_price, cand_sl)

        if VERBOSE_CLUSTER_DEBUG:
            log_strategy(
                cfg,
                f"[TRAIL_DBG] ticket={ticket} dir={info.direction} "
                f"open_R={open_R:.2f} HH={fmt_price(highest_high)} "
                f"LL={fmt_price(lowest_low)} ATR={atr_val:.2f} "
                f"cand_SL={fmt_price(cand_sl)} cur_SL={fmt_price(info.sl_price)}",
            )

        # 6) Enforce broker stop level
        ot      = mt5.ORDER_TYPE_BUY if info.direction == "buy" else mt5.ORDER_TYPE_SELL
        new_sl, _ = enforce_stop_level(ot, current_px, new_sl, info.tp_price)

        # Safety guards: SL must be on correct side, min 3 points away
        if info.direction == "buy":
            if new_sl >= current_px:
                continue
            if current_px - new_sl < 3 * point:
                continue
        else:
            if new_sl <= current_px:
                continue
            if new_sl - current_px < 3 * point:
                continue

        # 7) Only send modify if SL moved meaningfully (> 2 points)
        if abs(new_sl - info.sl_price) > 2 * point:
            modify_sl_tp(ticket, cfg, new_sl, info.tp_price)
            info.sl_price = new_sl


# ─────────────────────────────────────────────
# TIME EXITS
# ─────────────────────────────────────────────

def manage_time_exits(state: "StrategyState") -> None:
    """
    Close positions that have exceeded hold_minutes (if use_time_exit is True).
    """
    cfg = state.config
    if not cfg.use_time_exit or cfg.hold_minutes <= 0:
        return

    now = _utc_now()
    for ticket, info in list(state.open_positions.items()):
        elapsed = (now - info.entry_time).total_seconds() / 60.0
        if elapsed >= cfg.hold_minutes:
            log_strategy(cfg, f"[TIME_EXIT] ticket={ticket} elapsed={elapsed:.1f}min")
            close_position(ticket, cfg, reason="TimeExit")
            state.open_positions.pop(ticket, None)
