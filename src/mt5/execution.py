# src/mt5/execution.py
"""
All MT5 order execution and position management.

Functions:
  place_pending_entry()        — place a BUY_LIMIT or SELL_LIMIT
  close_position()             — market-close a position by ticket
  modify_sl_tp()               — update SL/TP on a live position
  get_positions_for_strategy() — snapshot of live positions by magic
  refresh_and_log_closes()     — diff prev vs current positions; log fills and closes
  infer_close_reason()         — look up close reason from MT5 deal history
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, TYPE_CHECKING

import MetaTrader5 as mt5

from config.config import MT5_SYMBOL, make_comment
from src.core.models import BotPositionInfo, PendingOrderMeta
from src.core.logger import log, log_strategy
from src.core.risk import calc_lot_size
from src.core.indicators import fetch_m1_rates, compute_atr
import src.mt5.connection as conn

if TYPE_CHECKING:
    from src.core.models import StrategyConfig, StrategyState

# ── Tracks reasons for positions closed by our own code ─────────────────────
# Consumed by refresh_and_log_closes() to avoid re-querying history.
RECENT_CLOSED_REASONS: Dict[int, str] = {}


# ─────────────────────────────────────────────
# PRICE / LOT HELPERS  (need SYMBOL_INFO)
# ─────────────────────────────────────────────

def round_price(x: float) -> float:
    return round(float(x), conn.SYMBOL_INFO.digits)


def fmt_price(x: float) -> str:
    return f"{float(x):.{conn.SYMBOL_INFO.digits}f}"


def fmt_delta(x: float) -> str:
    return f"{x:+.{conn.SYMBOL_INFO.digits}f}"


def enforce_stop_level(order_type: int, price: float, sl: float, tp: Optional[float]):
    """
    Widen SL/TP if they are inside the broker's minimum stop-level distance.
    Returns (sl, tp).
    """
    sl_level = getattr(conn.SYMBOL_INFO, "trade_stops_level", 0)
    point    = conn.SYMBOL_INFO.point

    if sl_level and sl_level > 0:
        min_dist = sl_level * point
        if order_type == mt5.ORDER_TYPE_BUY:
            if price - sl < min_dist:
                sl = price - min_dist
            if tp is not None and tp - price < min_dist:
                tp = price + min_dist
        else:
            if sl - price < min_dist:
                sl = price + min_dist
            if tp is not None and price - tp < min_dist:
                tp = price - min_dist

    sl = round_price(sl)
    tp = round_price(tp) if tp is not None else None
    return sl, tp


def calc_sl_tp(
    side: str,
    entry_price: float,
    cfg: "StrategyConfig",
    atr_val: Optional[float],
) -> Tuple[float, Optional[float]]:
    """
    Compute initial SL and TP prices from ATR (or fixed fallback).
    """
    if cfg.stop_mode == "fixed" or atr_val is None:
        sl_dist = cfg.sl_distance
    else:
        sl_dist = cfg.atr_init_mult * atr_val

    sl = entry_price - sl_dist if side == "buy" else entry_price + sl_dist

    tp = None
    if cfg.use_tp_exit:
        tp_dist = cfg.tp_R_multiple * sl_dist
        tp = entry_price + tp_dist if side == "buy" else entry_price - tp_dist

    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    sl, tp = enforce_stop_level(order_type, entry_price, sl, tp)
    return sl, tp


# ─────────────────────────────────────────────
# PLACE PENDING ENTRY
# ─────────────────────────────────────────────

def place_pending_entry(
    trade_side: str,
    cfg: "StrategyConfig",
    offset_dollars: float,
    trade_mode: str = "inverse",
) -> Optional[Tuple[int, PendingOrderMeta]]:
    """
    Place a BUY_LIMIT or SELL_LIMIT at a slight offset from current price.

    trade_side = "buy"  → BUY_LIMIT  @ bid − offset_dollars  (cheaper entry)
    trade_side = "sell" → SELL_LIMIT @ ask + offset_dollars  (higher entry for sell)

    trade_mode: "inverse" | "momentum"  — logged only, no execution difference.

    Returns (ticket, PendingOrderMeta) on success, None on failure.
    """
    tick = mt5.symbol_info_tick(MT5_SYMBOL)

    # ── Tick fallback ──────────────────────────────────────────────────────
    if tick is None or tick.bid <= 0 or tick.ask <= 0:
        log_strategy(cfg, f"[WARN] No live tick — falling back to last M1 close", level="WARN")
        try:
            df_last   = fetch_m1_rates(MT5_SYMBOL, bars=1)
            last_close = float(df_last["close"].iloc[-1])

            class _FakeTick:
                bid = last_close
                ask = last_close

            tick = _FakeTick()
        except Exception as e:
            log_strategy(cfg, f"[ERROR] Tick fallback failed: {e}", level="ERROR")
            return None

    bid          = float(tick.bid)
    ask          = float(tick.ask)
    market_price = (bid + ask) / 2.0

    # ── Limit entry price ──────────────────────────────────────────────────
    if trade_side == "buy":
        entry_price = round_price(bid - offset_dollars)
        order_type  = mt5.ORDER_TYPE_BUY_LIMIT
    else:
        entry_price = round_price(ask + offset_dollars)
        order_type  = mt5.ORDER_TYPE_SELL_LIMIT

    # ── ATR for SL/TP ──────────────────────────────────────────────────────
    atr_val = None
    if cfg.stop_mode in ("atr_static", "atr_trailing", "chandelier"):
        try:
            bars  = max(cfg.atr_period + 20, (cfg.chan_lookback or 0) + 5)
            df    = fetch_m1_rates(MT5_SYMBOL, bars=bars)
            atr_val = compute_atr(df, cfg.atr_period)
        except Exception as e:
            log_strategy(cfg, f"[WARN] ATR fetch failed: {e} — using fixed sl_distance", level="WARN")

    sl_price, tp_price = calc_sl_tp(trade_side, entry_price, cfg, atr_val)
    sl_distance        = abs(entry_price - sl_price)

    lots = calc_lot_size(sl_distance, cfg, conn.SYMBOL_INFO)
    if lots <= 0:
        log_strategy(cfg, "[ERROR] Lot size <= 0 — skipping entry", level="ERROR")
        return None

    # Enforce stop level relative to entry price
    base_type           = mt5.ORDER_TYPE_BUY if trade_side == "buy" else mt5.ORDER_TYPE_SELL
    sl_price, tp_price  = enforce_stop_level(base_type, entry_price, sl_price, tp_price)

    req = {
        "action":      mt5.TRADE_ACTION_PENDING,
        "symbol":      MT5_SYMBOL,
        "volume":      lots,
        "type":        order_type,
        "price":       entry_price,
        "sl":          sl_price,
        "tp":          tp_price if tp_price is not None else 0.0,
        "deviation":   50,
        "magic":       cfg.magic,
        "comment":     make_comment(f"{cfg.name}-{trade_mode[:3]}"),
        "type_time":   mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    res = mt5.order_send(req)

    if res is None:
        code, msg = mt5.last_error()
        log_strategy(cfg, f"[ERROR] place_pending: order_send=None, last_error={code} {msg}", level="ERROR")
        return None

    if res.retcode != mt5.TRADE_RETCODE_DONE:
        log_strategy(
            cfg,
            f"[ERROR] place_pending: retcode={res.retcode} comment={getattr(res,'comment',None)}",
            level="ERROR",
            retcode=res.retcode,
        )
        return None

    meta = PendingOrderMeta(
        created_at_utc=datetime.now(__import__("pytz").timezone("UTC")),
        trade_side=trade_side,
        pending_price=float(entry_price),
        market_price=float(market_price),
        trade_mode=trade_mode,
    )

    ticket = res.order
    log_strategy(
        cfg,
        f"[OK] PENDING {trade_side.upper()} [{trade_mode}] "
        f"mkt={fmt_price(market_price)} "
        f"bid={fmt_price(bid)} ask={fmt_price(ask)} "
        f"entry={fmt_price(entry_price)} SL={fmt_price(sl_price)} "
        f"TP={fmt_price(tp_price) if tp_price else 'None'} "
        f"lots={lots:.2f} ticket={ticket}",
        trade_side=trade_side,
        trade_mode=trade_mode,
        market_price=market_price,
        entry=entry_price,
        sl=sl_price,
        tp=tp_price,
        lots=lots,
        ticket=ticket,
    )
    return ticket, meta


# ─────────────────────────────────────────────
# CLOSE POSITION
# ─────────────────────────────────────────────

def close_position(ticket: int, cfg: "StrategyConfig", reason: str = "Exit") -> None:
    """
    Market-close a specific MT5 position by ticket + magic validation.
    Records the reason in RECENT_CLOSED_REASONS so it doesn't get
    double-looked-up via history.
    """
    poss = mt5.positions_get()
    pos  = None
    if poss:
        for p in poss:
            if p.ticket == ticket and p.magic == cfg.magic:
                pos = p
                break
    if pos is None:
        return  # already gone

    action = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    tick   = mt5.symbol_info_tick(MT5_SYMBOL)
    if tick is None:
        log_strategy(cfg, f"[ERROR] No tick when closing ticket={ticket}", level="ERROR")
        return

    price = round_price(tick.bid if action == mt5.ORDER_TYPE_SELL else tick.ask)

    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       MT5_SYMBOL,
        "volume":       pos.volume,
        "type":         action,
        "position":     ticket,
        "price":        price,
        "deviation":    20,
        "magic":        cfg.magic,
        "comment":      make_comment(f"{cfg.name}_{reason}"),
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    res = mt5.order_send(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        log_strategy(
            cfg,
            f"[ERROR] close_position failed ticket={ticket} "
            f"retcode={getattr(res,'retcode',None)}",
            level="ERROR",
            ticket=ticket,
            retcode=getattr(res, "retcode", None),
        )
    else:
        RECENT_CLOSED_REASONS[ticket] = reason
        log_strategy(
            cfg,
            f"[OK] Closed ticket={ticket} @ {fmt_price(price)} reason={reason}",
            ticket=ticket, price=price, reason=reason,
        )


# ─────────────────────────────────────────────
# MODIFY SL/TP
# ─────────────────────────────────────────────

def modify_sl_tp(
    ticket: int,
    cfg: "StrategyConfig",
    new_sl: float,
    new_tp: Optional[float],
) -> None:
    """Send a TRADE_ACTION_SLTP request to update SL and/or TP."""
    poss = mt5.positions_get()
    pos  = None
    if poss:
        for p in poss:
            if p.ticket == ticket and p.magic == cfg.magic:
                pos = p
                break
    if pos is None:
        return

    req = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   MT5_SYMBOL,
        "position": ticket,
        "sl":       new_sl,
        "tp":       new_tp if new_tp is not None else 0.0,
        "magic":    cfg.magic,
        "comment":  make_comment(f"{cfg.name}_SLmod"),
    }
    res = mt5.order_send(req)

    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        log_strategy(
            cfg,
            f"[WARN] modify_sl_tp failed ticket={ticket} "
            f"retcode={getattr(res,'retcode',None)}",
            level="WARN", ticket=ticket,
        )
    else:
        log_strategy(
            cfg,
            f"[OK] SL moved ticket={ticket} → SL={fmt_price(new_sl)} "
            f"TP={fmt_price(new_tp) if new_tp else 'None'}",
            ticket=ticket, new_sl=new_sl, new_tp=new_tp,
        )


# ─────────────────────────────────────────────
# POSITION SNAPSHOT
# ─────────────────────────────────────────────

def get_positions_for_strategy(cfg: "StrategyConfig") -> Dict[int, BotPositionInfo]:
    """Return all live MT5 positions matching this strategy's magic number."""
    poss   = mt5.positions_get(symbol=MT5_SYMBOL)
    result: Dict[int, BotPositionInfo] = {}
    if not poss:
        return result

    for p in poss:
        if p.magic != cfg.magic:
            continue
        direction  = "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell"
        entry_time = datetime.fromtimestamp(p.time, tz=__import__("pytz").timezone("UTC"))
        result[p.ticket] = BotPositionInfo(
            ticket=p.ticket,
            direction=direction,
            entry_time=entry_time,
            entry_price=p.price_open,
            sl_price=p.sl,
            tp_price=p.tp if p.tp > 0 else None,
            initial_sl_price=p.sl,  # will be overwritten in refresh if already tracked
        )
    return result


# ─────────────────────────────────────────────
# INFER CLOSE REASON FROM HISTORY
# ─────────────────────────────────────────────

def infer_close_reason(ticket: int) -> str:
    """
    Best-effort: look up the close reason from MT5 deal history.
    Returns a short string like "TP (price=2100.50, profit=+85.00)".
    """
    try:
        from pytz import timezone as tz
        end   = datetime.now(tz("UTC"))
        start = end - timedelta(days=3)

        deals = mt5.history_deals_get(start, end)
        if not deals:
            return "unknown"

        closing = [
            d for d in deals
            if getattr(d, "position_id", None) == ticket
            and getattr(d, "entry", None) in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)
        ]
        if not closing:
            return "unknown"

        last    = closing[-1]
        profit  = getattr(last, "profit", 0.0)
        price   = getattr(last, "price",  0.0)
        code    = getattr(last, "reason", None)

        reason_map = {
            mt5.DEAL_REASON_SL:       "SL",
            mt5.DEAL_REASON_TP:       "TP",
            mt5.DEAL_REASON_SO:       "StopOut",
            mt5.DEAL_REASON_CLIENT:   "Manual",
            mt5.DEAL_REASON_EXPERT:   "Expert",
            mt5.DEAL_REASON_MARGINAL: "Margin",
        }
        base = reason_map.get(code, f"Other({code})")
        return f"{base} (price={price}, profit={profit:+.2f})"

    except Exception:
        return "unknown"


# ─────────────────────────────────────────────
# REFRESH STATE + LOG FILLS & CLOSES
# ─────────────────────────────────────────────

def refresh_and_log_closes(state: "StrategyState") -> None:
    """
    Sync state.open_positions with live MT5 snapshot.
    Logs:
      - newly filled pending orders (OPENED)
      - positions that disappeared since last call (CLOSED, with reason)
    Preserves initial_sl_price across loops.
    """
    from pytz import timezone
    from config.config import TRADE_COOLDOWN_SECONDS
    from src.core.logger import log_strategy

    cfg  = state.config
    prev = state.open_positions
    curr = get_positions_for_strategy(cfg)

    prev_tickets = set(prev.keys())
    curr_tickets = set(curr.keys())

    # ── New fills ─────────────────────────────────────────────────────────
    for ticket in curr_tickets - prev_tickets:
        info = curr[ticket]

        # Compute fill quality (d1 = vs market, d2 = vs limit price)
        d1_str = d2_str = "NA"
        meta   = state.pending_meta.pop(ticket, None)
        if meta is not None:
            entry  = float(info.entry_price)
            if info.direction == "buy":
                d1 = meta.market_price  - entry
                d2 = meta.pending_price - entry
            else:
                d1 = entry - meta.market_price
                d2 = entry - meta.pending_price
            d1_str = fmt_delta(d1)
            d2_str = fmt_delta(d2)
            # Carry trade_mode forward into position info
            info.trade_mode = meta.trade_mode

        log_strategy(
            cfg,
            f"[OPENED] ticket={ticket} dir={info.direction} "
            f"mode={info.trade_mode} "
            f"entry={fmt_price(info.entry_price)} "
            f"SL={fmt_price(info.sl_price)} "
            f"TP={fmt_price(info.tp_price) if info.tp_price else 'None'} "
            f"{{d1={d1_str} d2={d2_str}}}",
            ticket=ticket,
            direction=info.direction,
            trade_mode=info.trade_mode,
            d1=d1_str, d2=d2_str,
        )

        # Remove the matching pending order tracking entry
        state.pending_orders.pop(ticket, None)

        # Start cooldown from fill
        from datetime import timedelta
        now = datetime.now(timezone("UTC"))
        state.cooldown_until_utc = now + timedelta(seconds=TRADE_COOLDOWN_SECONDS)

        # Reset cluster buffer so we only react to NEW clusters after this fill
        state.cluster_engine.events.clear()
        state.cluster_engine.last_cluster_time = None
        state.cluster_engine.last_cluster_side = None

    # ── Closes ────────────────────────────────────────────────────────────
    for ticket in prev_tickets - curr_tickets:
        info   = prev[ticket]
        reason = RECENT_CLOSED_REASONS.pop(ticket, None) or infer_close_reason(ticket)
        log_strategy(
            cfg,
            f"[CLOSED] ticket={ticket} dir={info.direction} mode={info.trade_mode} "
            f"entry={fmt_price(info.entry_price)} reason={reason}",
            ticket=ticket,
            direction=info.direction,
            trade_mode=info.trade_mode,
            closed_reason=reason,
        )

    # ── Merge — preserve initial_sl_price and trade_mode across loops ─────
    merged: Dict[int, BotPositionInfo] = {}
    for ticket, cur in curr.items():
        prev_info = prev.get(ticket)
        if prev_info is not None:
            cur.initial_sl_price = prev_info.initial_sl_price
            cur.trade_mode       = prev_info.trade_mode
            cur.breakeven_hit    = prev_info.breakeven_hit
        else:
            cur.initial_sl_price = cur.sl_price
        merged[ticket] = cur

    state.open_positions = merged
