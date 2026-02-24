# XAU SiRiX Cluster Hybrid Bot

A structured, modular MT5 trading bot for **XAUUSD** that:
- Polls SiRiX prop-firm open positions to detect trader clusters
- Uses a **hybrid direction model** (inverse *or* momentum) decided per-cluster via RSI + VWAP
- Places limit orders for better fill prices in both directions
- Manages positions with a **chandelier trailing stop** + optional **breakeven SL move**
- Enforces daily loss limits, no-trade zones, and MT5 auto-reconnect

---

## Project Structure

```
ALGO_XAU-SIRIX-Cluster/               ← project root (content root in PyCharm)
│
├── config/
│   ├── config.py                ← ALL global constants (credentials, limits, timing)
│   └── strategies.yaml          ← strategy parameters (edit here, not in code)
│
├── src/
│   ├── main.py                  ← entry point + main loop (thin orchestrator)
│   │
│   ├── core/
│   │   ├── models.py            ← dataclasses: SirixPositionEvent, BotPositionInfo, etc.
│   │   ├── cluster_engine.py    ← rolling-window cluster detection
│   │   ├── indicators.py        ← ATR, RSI, VWAP (pure functions)
│   │   ├── logger.py            ← structured JSONL logging (double-log bug fixed)
│   │   ├── filters.py           ← session filter + no-trade zones
│   │   ├── risk.py              ← lot sizing + daily loss circuit breaker
│   │   └── state.py             ← JSON state snapshot writer
│   │
│   ├── mt5/
│   │   ├── connection.py        ← MT5 init + auto-reconnect
│   │   └── execution.py         ← order placement, close, modify SL/TP
│   │
│   ├── sirix/
│   │   └── api.py               ← SiRiX REST API fetch + event parsing
│   │
│   └── strategies/
│       ├── loader.py            ← reads strategies.yaml → StrategyState list
│       └── chandelier.py        ← chandelier logic + hybrid direction decision
│
├── logs/                        ← auto-created; bot_log.jsonl written here
├── state/                       ← auto-created; bot_state.json written here
├── no_trade_zones.json          ← add date/time windows to pause trading
├── requirements.txt
└── README.md
```

---

## How to Run

```powershell
# From the project root (ALGO_XAU-SIRIX-Cluster/)
python -m src.main
```

No command-line arguments. All configuration is in `config/config.py` and `config/strategies.yaml`.

---

## Configuration

### `config/config.py` — Global Settings

Edit these sections as needed:

| Section | Key variables |
|---|---|
| MT5 account | `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `MT5_TERMINAL_PATH` |
| Symbol | `MT5_SYMBOL` |
| SiRiX API | `SIRIX_TOKEN`, `SIRIX_GROUPS` |
| Timing | `POLL_INTERVAL_SECONDS`, `TRADE_COOLDOWN_SECONDS`, `PENDING_ORDER_TIMEOUT_MIN` |
| Session filter | `USE_SESSION_FILTER`, `SESSION_START_HHMM`, `SESSION_END_HHMM` |
| Daily limits | `DAILY_LOSS_LIMIT_TOTAL`, `DAILY_LOSS_LIMIT_PER_ENGINE` |
| Verbosity | `VERBOSE_CLUSTERS`, `VERBOSE_HYBRID`, `VERBOSE_CLUSTER_DEBUG` |

### `config/strategies.yaml` — Strategy Parameters

All per-strategy tunable parameters live here. The bot reads this file at startup — **no code changes needed** when adjusting strategy parameters.

#### Key parameters explained:

```yaml
t_seconds: 30        # rolling window: a cluster fires if K unique traders
k_unique: 3          # open in the same direction within T seconds of each other

direction_mode: "hybrid"   # "inverse" | "momentum" | "hybrid"

# Hybrid thresholds:
rsi_overbought: 65.0   # RSI above this → bearish momentum confirmed
rsi_oversold:   35.0   # RSI below this → bullish momentum confirmed
vwap_band_pct: 0.001   # price must be 0.1% away from VWAP to count

limit_offset_dollars: 2.0  # BUY_LIMIT = bid - $2;  SELL_LIMIT = ask + $2

breakeven_trigger_R: 0.5   # move SL to entry once +0.5R profit is reached
trail_start_R: 0.3          # chandelier trail activates at +0.3R profit
```

---

## Hybrid Direction Model

The key innovation over the original script. Instead of *always* fading the cluster, the bot uses **RSI + VWAP** to decide per-cluster whether to go inverse or with the crowd.

### Decision Logic

```
Cluster detected (e.g. SELL cluster: ≥K traders opened SELL within T seconds)

IF direction_mode == "hybrid":

  SELL cluster → go WITH (sell) if BOTH:
    · RSI > rsi_overbought   (65)  → price is overbought, real selling pressure
    · price > VWAP × (1 + 0.001)  → price extended above daily anchor

  BUY cluster → go WITH (buy) if BOTH:
    · RSI < rsi_oversold     (35)  → price is oversold, real buying pressure
    · price < VWAP × (1 - 0.001)  → price extended below daily anchor

  Otherwise → INVERSE (fade the crowd as original)
```

### Why RSI + VWAP together?

- **RSI alone** fires too often on choppy markets (frequent extremes in ranging conditions)
- **VWAP alone** is too slow — price can be above VWAP without real momentum
- **Together**: RSI catches the extreme; VWAP confirms the crowd is pushing into already-extended territory

### Entry limit orders (both modes)

Regardless of direction, entries are always limit orders:
- **BUY** → `BUY_LIMIT` at `bid − limit_offset_dollars` (better than chasing market)
- **SELL** → `SELL_LIMIT` at `ask + limit_offset_dollars` (better than chasing market)

This minimises spread cost and slippage in both inverse and momentum directions.

---

## Chandelier Stop + Breakeven

### Chandelier Trail
```
BUY position:
  candidate_SL = HH(last 30 M1 bars) − ATR(5) × atr_trail_mult(2.0)
  new_SL = max(current_SL, candidate_SL)  ← never moves backwards

SELL position:
  candidate_SL = LL(last 30 M1 bars) + ATR(5) × atr_trail_mult(2.0)
  new_SL = min(current_SL, candidate_SL)
```

Trail only activates once position is at `+trail_start_R` profit (default: +0.3R).

### Breakeven
Once position reaches `+breakeven_trigger_R` profit (default: +0.5R), SL is moved to entry price. This runs **before** the chandelier trail check, so the trail never pushes SL back below entry.

---

## Risk Sizing

| Mode | Formula |
|---|---|
| `dynamic_pct` | `lots = (equity × risk_percent) ÷ (sl_distance × contract_size)` |
| `static_pct` | `lots = (static_risk_base_balance × risk_percent) ÷ (sl_distance × contract_size)` |
| `fixed_lots` | `lots = fixed_lots` (always) |

Default: `dynamic_pct` at `1%` per trade.

---

## Circuit Breakers

| Breaker | Default | Behaviour on breach |
|---|---|---|
| Daily loss per engine | $500 | Flatten + stop bot |
| Daily total loss | $1000 | Flatten + stop bot |
| No-trade zone (JSON) | See file | Flatten + pause (resumes when zone ends) |
| Session filter | OFF | Skip entries outside hours |
| Trade cooldown | 120s | No new entries for 2 min after fill/placement |
| Pending TTL | 3 min | Cancel unfilled limit order |
| MT5 reconnect | 5 attempts × 10s | Halt trading if all fail |

---

## No-Trade Zones (`no_trade_zones.json`)

Place this file in the project root. The bot reloads it every loop — no restart needed when adding new zones.

### Format

```json
[
  {
    "start_local": "2025-06-10 08:00",
    "end_local":   "2025-06-10 10:00",
    "reason":      "FOMC"
  },
  {
    "start_local": "2025-06-15 13:30",
    "end_local":   "2025-06-15 14:30",
    "reason":      "NFP"
  }
]
```

### Field rules

| Field | Format | Timezone |
|---|---|---|
| `start_local` | `"YYYY-MM-DD HH:MM"` | Europe/London |
| `end_local` | `"YYYY-MM-DD HH:MM"` | Europe/London |
| `reason` | any string | Logged only, no functional effect |

### What happens when a zone is active

1. Bot detects the zone at the top of the main loop.
2. All open positions are closed at market.
3. All pending limit orders are cancelled.
4. Cluster engine buffers are cleared (so only fresh signals trigger entries after resuming).
5. Bot sleeps and rechecks every second until the zone ends — then resumes automatically.

### Common mistakes that will break the file

| Mistake | Example | Effect |
|---|---|---|
| `T` separator instead of space | `"2025-06-10T08:00"` | Zone silently skipped |
| Seconds included | `"2025-06-10 08:00:00"` | Zone silently skipped |
| Single object instead of array | `{ ... }` | Entire file ignored |
| Trailing comma on last entry | `}, ]` | JSON parse error, file ignored |
| Missing quotes around values | `start_local: 2025-06-10 08:00` | JSON parse error |

### Empty file (no active zones)

If you have no upcoming events, keep the file as an empty array — the bot loads it cleanly and no zone is ever entered:

```json
[]
```

---

## Adding a Second Bot (New Account / New Strategies)

1. Add a new top-level key in `strategies.yaml`:

```yaml
xau_second_bot:
  - name: "MyNewStrategy"
    magic: 990001
    enabled: true
    direction_mode: "inverse"
    t_seconds: 45
    ...
```

2. Create a new entry script (e.g. `src/main_second.py`):

```python
# src/main_second.py
from config.config import BOT_NAME, LOG_PATH, STATE_PATH
from src.core.logger import init_logger, enable_print_capture
from src.strategies.loader import load_strategies
# ... same structure as src/main.py but with bot_key="xau_second_bot"
```

3. Run it with a different MT5 account set in `config.py` (or a second `config_second.py`).

The entire `src/core/`, `src/mt5/`, `src/sirix/`, and `src/strategies/` infrastructure is reused. Only the YAML key and credentials change.

---

## Logs

All logs are written to `logs/bot_log.jsonl` as one JSON object per line:

```json
{"ts_utc": "2025-06-01T09:15:32Z", "level": "INFO", "strategy": "Chandelier_Hybrid", "magic": 880003, "msg": "[HYBRID] cluster=buy RSI=29.4 VWAP=2310.50 price=2307.20 → mode=momentum trade_side=buy"}
{"ts_utc": "2025-06-01T09:15:33Z", "level": "INFO", "strategy": "Chandelier_Hybrid", "magic": 880003, "msg": "[OK] PENDING BUY [momentum] entry=2305.20 SL=2299.10 TP=2311.30 lots=0.12 ticket=12345678"}
```

This format is directly queryable with `pandas`, `jq`, or any log aggregation tool.

---

## Files You Should Never Need to Edit

| File | Why |
|---|---|
| `src/core/models.py` | Data containers only |
| `src/core/cluster_engine.py` | Generic cluster logic |
| `src/core/indicators.py` | Pure math functions |
| `src/core/logger.py` | Logging infrastructure |
| `src/core/filters.py` | Generic gate checks |
| `src/core/state.py` | State serialisation |
| `src/mt5/connection.py` | MT5 connection boilerplate |
| `src/sirix/api.py` | API plumbing |

---

## Files You Will Regularly Edit

| File | What to change |
|---|---|
| `config/config.py` | Credentials, risk limits, verbosity flags |
| `config/strategies.yaml` | Strategy parameters, RSI/VWAP thresholds, offsets |
| `no_trade_zones.json` | Add news events / maintenance windows |
| `src/strategies/chandelier.py` | Trailing logic, breakeven, hybrid thresholds logic |

---

## Dependencies

```
MetaTrader5
pandas
requests
pytz
pyyaml
```

Install:
```powershell
pip install MetaTrader5 pandas requests pytz pyyaml
```
