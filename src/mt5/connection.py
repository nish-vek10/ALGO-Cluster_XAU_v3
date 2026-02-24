# src/mt5/connection.py
"""
MT5 connection management.

Responsibilities:
  - Initialize MT5 terminal and select symbol.
  - Expose SYMBOL_INFO as a module-level global (set on init, never None after that).
  - Provide ensure_connected() for health-check + auto-reconnect in the main loop.
"""
from __future__ import annotations

import sys
import time

import MetaTrader5 as mt5

from config.config import (
    MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_TERMINAL_PATH, MT5_SYMBOL,
    MT5_MAX_RECONNECT_ATTEMPTS, MT5_RECONNECT_WAIT_SECONDS,
)
from src.core.logger import log

# ── Module-level symbol info (filled by init_mt5, never None after that) ─────
SYMBOL_INFO = None


def init_mt5() -> None:
    """
    Connect to MT5 terminal, verify account, select symbol.
    Exits the process on failure — no point running without a live connection.
    """
    global SYMBOL_INFO

    if not mt5.initialize(
        login=MT5_LOGIN,
        password=MT5_PASSWORD,
        server=MT5_SERVER,
        path=MT5_TERMINAL_PATH,
    ):
        log(f"[MT5] initialize() failed: {mt5.last_error()}", level="ERROR")
        sys.exit(1)

    acct = mt5.account_info()
    if acct is None:
        log(f"[MT5] account_info() is None: {mt5.last_error()}", level="ERROR")
        sys.exit(1)

    log(
        f"[MT5] Connected — login={acct.login} "
        f"balance={acct.balance:.2f} equity={acct.equity:.2f}"
    )

    # Ensure symbol is available and visible in Market Watch
    info = mt5.symbol_info(MT5_SYMBOL)
    if info is None:
        log(f"[MT5] Symbol not found: {MT5_SYMBOL}", level="ERROR")
        sys.exit(1)

    if not info.visible:
        if not mt5.symbol_select(MT5_SYMBOL, True):
            log(f"[MT5] Failed to select {MT5_SYMBOL}", level="ERROR")
            sys.exit(1)

    SYMBOL_INFO = mt5.symbol_info(MT5_SYMBOL)   # refresh after select

    log(
        f"[MT5] Symbol {MT5_SYMBOL}: digits={SYMBOL_INFO.digits} "
        f"contract={SYMBOL_INFO.trade_contract_size} "
        f"vol_min={SYMBOL_INFO.volume_min} vol_max={SYMBOL_INFO.volume_max} "
        f"step={SYMBOL_INFO.volume_step}"
    )


def ensure_connected() -> bool:
    """
    Lightweight health check called each main loop iteration.
    If MT5 has disconnected, attempts to reconnect up to
    MT5_MAX_RECONNECT_ATTEMPTS times before returning False.

    Returns True  → connection OK (or successfully restored)
    Returns False → reconnect failed (caller should halt trading)
    """
    if mt5.account_info() is not None:
        return True   # already connected — fast path

    log("[MT5] Connection lost — attempting reconnect …", level="WARN")

    for attempt in range(1, MT5_MAX_RECONNECT_ATTEMPTS + 1):
        mt5.shutdown()
        time.sleep(MT5_RECONNECT_WAIT_SECONDS)

        if mt5.initialize(
            login=MT5_LOGIN,
            password=MT5_PASSWORD,
            server=MT5_SERVER,
            path=MT5_TERMINAL_PATH,
        ):
            if mt5.account_info() is not None:
                log(f"[MT5] Reconnected on attempt {attempt}")
                return True

        log(
            f"[MT5] Reconnect attempt {attempt}/{MT5_MAX_RECONNECT_ATTEMPTS} failed",
            level="WARN",
        )

    log("[MT5] All reconnect attempts failed — halting trading", level="ERROR")
    return False
