# config/config.py
"""
Global bot configuration.
All non-strategy, non-per-trade constants live here.
Strategy-specific parameters live in config/strategies.yaml.

Edit this file to change: MT5 credentials, API keys, risk limits,
polling speed, session times, logging verbosity.
"""

from pathlib import Path
from pytz import timezone

# ─────────────────────────────────────────────
# BOT IDENTITY
# ─────────────────────────────────────────────

BOT_NAME    = "XAU_SiRiX_ClusterHybrid"
BOT_KEY     = "xau_cluster_bot-v3"        # must match top-level key in strategies.yaml
BASE_DIR    = Path(__file__).resolve().parent.parent

# ─────────────────────────────────────────────
# FILE PATHS  (auto-created if missing)
# ─────────────────────────────────────────────

LOGS_DIR              = BASE_DIR / "logs"
STATE_DIR             = BASE_DIR / "state"
LOG_PATH              = LOGS_DIR  / "bot_log.jsonl"
STATE_PATH            = STATE_DIR / "bot_state.json"
NO_TRADE_ZONES_PATH   = BASE_DIR  / "no_trade_zones.json"
STRATEGIES_YAML_PATH  = BASE_DIR  / "config" / "strategies.yaml"

# ─────────────────────────────────────────────
# MT5 ACCOUNT
# ─────────────────────────────────────────────

MT5_LOGIN         = 52759521
MT5_PASSWORD      = "2NYeNukk!FK7dh"
MT5_SERVER        = "ICMarketsSC-Demo"
MT5_TERMINAL_PATH = r"C:\MT5\TradeCopier-Cluster_XAU-v3\terminal64.exe"
MT5_SYMBOL        = "XAUUSD"

# Reconnect settings if MT5 drops mid-session
MT5_MAX_RECONNECT_ATTEMPTS = 5
MT5_RECONNECT_WAIT_SECONDS = 10

# ─────────────────────────────────────────────
# TIMEZONES
# ─────────────────────────────────────────────

LOCAL_TZ = timezone("Europe/London")
SIRIX_TZ = timezone("Asia/Jerusalem")   # SiRiX server clock

# ─────────────────────────────────────────────
# SIRIX REST API
# ─────────────────────────────────────────────

SIRIX_BASE_URL    = "https://restapi-real3.sirixtrader.com"
SIRIX_ENDPOINT    = "/api/ManagementService/GetOpenPositionsForGroups"
SIRIX_TOKEN       = "t1_a7xeQOJPnfBzuCncH60yjLFu"
SIRIX_GROUPS      = ["Audition", "Funded", "Purchases"]
SIRIX_INSTRUMENT  = "XAUUSD"
SIRIX_HTTP_TIMEOUT = 10   # seconds per request

# ─────────────────────────────────────────────
# MAIN LOOP TIMING
# ─────────────────────────────────────────────

POLL_INTERVAL_SECONDS       = 1     # main loop sleep
CLUSTER_REFRACTORY_SECONDS  = 1     # min gap between same-side clusters per engine
PENDING_ORDER_TIMEOUT_MIN   = 3     # cancel pending if not filled within N minutes
TRADE_COOLDOWN_SECONDS      = 120   # after fill or pending placement, wait this long

# Cap on seen_order_ids age to prevent unbounded memory growth
SEEN_ORDERS_MAX_AGE_HOURS   = 24

# ─────────────────────────────────────────────
# MT5 ORDER COMMENT
# ─────────────────────────────────────────────

MAX_COMMENT_LEN = 20

def make_comment(text: str) -> str:
    """Truncate comment to safe broker limit."""
    return str(text)[:MAX_COMMENT_LEN] if text else ""

# ─────────────────────────────────────────────
# SESSION FILTER  (optional trading hours gate)
# ─────────────────────────────────────────────

USE_SESSION_FILTER  = False
SESSION_START_HHMM  = "08:00"
SESSION_END_HHMM    = "18:00"

# ─────────────────────────────────────────────
# NO-TRADE ZONES  (JSON file override)
# ─────────────────────────────────────────────

USE_NO_TRADE_ZONES = True

# ─────────────────────────────────────────────
# DAILY LOSS CIRCUIT BREAKERS
# ─────────────────────────────────────────────

USE_DAILY_LOSS_LIMITS        = True
DAILY_LOSS_LIMIT_TOTAL       = 1000.0   # USD across ALL engines
DAILY_LOSS_LIMIT_PER_ENGINE  = 500.0    # USD per magic number

# ─────────────────────────────────────────────
# LOGGING VERBOSITY
# ─────────────────────────────────────────────

VERBOSE_CLUSTERS       = True    # log cluster detections
VERBOSE_CLUSTER_DEBUG  = False   # noisy per-loop cluster stats
VERBOSE_HYBRID         = True    # log RSI/VWAP values at each decision

# Heartbeat intervals (0 = OFF)
COOLDOWN_HEARTBEAT_SECONDS = 0
NO_TRADE_HEARTBEAT_SECONDS = 0
EQUITY_HEARTBEAT_SECONDS   = 600  # print equity/floating PnL every 10 min
