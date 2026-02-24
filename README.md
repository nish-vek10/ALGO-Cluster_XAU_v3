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
rsi_overbought: 70.0   # RSI above this → bearish momentum confirmed
rsi_oversold:   30.0   # RSI below this → bullish momentum confirmed
vwap_band_pct: 0.001   # price must be 0.1% away from VWAP to count

limit_offset_dollars: 1.0  # BUY_LIMIT = bid - $1;  SELL_LIMIT = ask + $2

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
    · RSI > rsi_overbought   (70)  → price is overbought, real selling pressure
    · price > VWAP × (1 + 0.001)  → price extended above daily anchor

  BUY cluster → go WITH (buy) if BOTH:
    · RSI < rsi_oversold     (30)  → price is oversold, real buying pressure
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

## Understanding the Indicators

### RSI — Relative Strength Index

RSI acts as a **fatigue meter for price**. It measures how hard and fast price has moved over the last `rsi_period` M1 candles (default: 14 minutes), on a scale of 0 to 100.

| RSI Range | What it means |
|---|---|
| Above 70 | Price has been running up aggressively — "overbought", statistically stretched |
| Below 30 | Price has been selling off aggressively — "oversold", stretched to the downside |
| 30 to 70 | No strong conviction — choppy or ranging, no clear momentum |

When RSI is above 70 and a **SELL** cluster fires, the crowd is selling into an already-extended move. That is a situation where they *may be right* — not just noise. **The bot considers going with them**.

When RSI is between 30 and 70 (no extreme), the crowd is more likely making an emotional or random entry. **The bot fades them as usual**.

---

### VWAP — Volume Weighted Average Price

VWAP is the **fair value anchor for the day**. It calculates the average price that every unit of gold has traded at since midnight UTC, weighted by volume at each level. It resets daily.

| Price vs VWAP | What it means |
|---|---|
| Significantly above VWAP | Buyers in control all day — bullish daily bias, price is extended above fair value |
| Significantly below VWAP | Sellers in control — bearish daily bias, price is extended below fair value |
| Near VWAP | Market is balanced — no strong daily directional bias |

"Significantly" is defined by `vwap_band_pct: 0.001` — price must be at least **0.1% from VWAP** before the condition counts. At gold priced at $5,000 this is roughly **$5.00** of separation.

VWAP is deliberately slower than RSI. It reflects the *entire day's* trading activity and is much harder to fake than a short-term RSI spike.

---

### Why RSI and VWAP Together

Each indicator has a weakness in isolation:

- **RSI alone** fires too often. On choppy days RSI can touch 70 and 30 repeatedly with no real momentum behind the move — you end up following weak clusters with no follow-through
- **VWAP alone** is too slow. Price can sit above VWAP without any particular urgency — it does not tell you whether a genuine push is happening *right now*

**Together they form a two-key lock:**

> RSI says → *"right now, in the last 14 minutes, the move is extreme"*
> VWAP says → *"and the whole day supports this direction"*

Only when both keys agree does the bot classify a cluster as genuine momentum.

---

### Full Entry Decision Flow (Hybrid Mode)
```
1. CLUSTER DETECTED
   ≥ 3 unique SiRiX traders open in the same direction within 30 seconds

2. HYBRID CHECK (fetch last 300 M1 candles → compute RSI + VWAP)

   SELL cluster fires:
     Is RSI > 70?               (price overbought right now)
     Is price > VWAP + 0.1%?    (daily bias is bullish, price is extended up)
     ─────────────────────────────────────────────────
     BOTH yes → crowd is selling into genuine overextension     → GO WITH → SELL
     Either no → crowd is likely wrong / early                  → FADE    → BUY

   BUY cluster fires:
     Is RSI < 30?               (price oversold right now)
     Is price < VWAP − 0.1%?    (daily bias is bearish, price is extended down)
     ─────────────────────────────────────────────────
     BOTH yes → crowd is buying into genuine oversold bounce    → GO WITH → BUY
     Either no → crowd is likely wrong / early                  → FADE    → SELL

3. LIMIT ORDER PLACED (never a market order)

   Going BUY  → BUY_LIMIT  @ bid − $1.00
                Order waits for a $1.00 dip before filling
                Expires and cancels after 3 minutes if not filled

   Going SELL → SELL_LIMIT @ ask + $1.00
                Order waits for a $1.00 spike before filling
                Expires and cancels after 3 minutes if not filled

   Why limit orders in both directions?
   Even when following momentum, entering on a micro-retracement rather than
   chasing the spike gives a meaningfully better average entry price over time.
   The $1.00 offset is small relative to the SL distance but improves the
   effective R:R on every trade that fills.

4. POSITION MANAGED (see Chandelier Stop + Breakeven section below)
   · Chandelier trail activates at +0.3R profit
   · SL moves to breakeven at +0.5R profit
   · Fixed TP at 1.5R
```

---

### Trade Identification by Mode

Every trade is tagged with its direction decision for post-analysis:

| Where | Tag |
|---|---|
| MT5 History → Comment column | `Chand_Hybrid_inv` or `Chand_Hybrid_mom` |
| `logs/bot_log.jsonl` | `"trade_mode": "inverse"` or `"trade_mode": "momentum"` |
| `state/bot_state.json` | `"trade_mode"` field on every live position |

Monthly analysis example (Python):
```python
import pandas as pd
df     = pd.read_json("logs/bot_log.jsonl", lines=True)
closed = df[df["msg"].str.startswith("[CLOSED]")]
print(closed.groupby("trade_mode")["closed_reason"].value_counts())
```

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
{"ts_utc": "2025-06-01T09:15:32Z", "level": "INFO", "strategy": "CH-HYBRID", "magic": 880003, "msg": "[HYBRID] cluster=buy RSI=29.4 VWAP=2310.50 price=2307.20 → mode=momentum trade_side=buy"}
{"ts_utc": "2025-06-01T09:15:33Z", "level": "INFO", "strategy": "CH-HYBRID", "magic": 880003, "msg": "[OK] PENDING BUY [momentum] entry=2305.20 SL=2299.10 TP=2311.30 lots=0.12 ticket=12345678"}
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
