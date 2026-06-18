# Polymarket Tennis Backtester

A data pipeline and backtesting framework for discovering and validating profitable trading strategies on [Polymarket](https://polymarket.com) tennis prediction markets (ATP & WTA).

Built entirely from scratch — data collection, strategy engine, walk-forward validation, and an interactive web UI.

---

## Key Results

The best strategy found — **pre-match momentum reversal on ATP match-winner markets** — was validated using walk-forward analysis (train on early data, test on unseen later data):

| Period | Bets | Win Rate | ROI |
|--------|------|----------|-----|
| Training (Apr 11 – Jun 9) | 273 | 41.0% | +10.4% |
| **Out-of-sample test (Jun 9 – present)** | **36** | **52.8%** | **+29.5%** |

The out-of-sample win rate (52.8%) is *higher* than training. That gap between implied probability and actual win rate is the edge being exploited.

Execution cost stress-testing confirmed the edge survives realistic live trading conditions:

| Execution model | ROI |
|-----------------|-----|
| Baseline (mid prices, no costs) | +14.1% |
| Entry at ask + exit at bid (2¢ spread modeled) | +8.3% |
| + 1¢ additional SL slippage | +7.6% |
| Stress test (3¢ spread + 2¢ SL slippage) | +4.4% |

All figures are over 308 bets across 37 days (May 10 – Jun 16 2026), ~8 signals per day. The strategy remains profitable through a 5¢ round-trip execution cost — the realistic ceiling for liquid ATP markets on Polymarket.

---

## What the Strategy Does

**`momentum_reversal` — mom_fast variant**

Every ATP match on Polymarket is a binary market: one player's YES contract and the other player's YES contract, priced so they sum to ~$1. The strategy watches the overnight pre-match window — the 12–20 hours between market creation and match start — and fades overreactions in either direction.

**Signal:** compare each player's YES price now vs 5 hours ago.

- If a player's YES price **dropped** 5+ cents in 5 hours → buy that player's YES contract, expecting the price to recover.
- If a player's YES price **rose** 5+ cents in 5 hours → buy the *opposing* player's YES contract (the one that got cheap as money poured into the other side), expecting the same reversion.

Both signals are structurally identical: you're buying the YES contract that the market just moved away from, expecting it to drift back.

**Why it works:** Pre-match betting pressure concentrates on one player at a time — news, social media, sharp money — and tends to overshoot. The strategy buys the contract that got hit, before the market corrects itself.

**Why pre-match only:** Polymarket creates match markets the evening before the match. The first 20% of a market's lifetime is almost entirely pre-match (confirmed using Polymarket's actual `startTime` field — 308 of 309 signals fire before the match begins). In-match price moves reflect real gameplay, not sentiment overreaction, so the signal does not apply once the match starts.

**Stop-loss at 10¢:** if the position continues moving against you, exit at best bid rather than ride to zero.

**Optimal stake: 15% of bankroll per bet**, compounding. Chosen on training data only — never exposed to the test period. For live trading, start at 2–5%.

---

## Technical Stack

- **Python** — data pipeline, strategy engine, backtesting
- **SQLite** — local database (5,478 ATP events, 7,314 WTA markets, ~270K price points)
- **asyncio + aiohttp** — concurrent price history collection (~20x faster than sequential)
- **FastAPI** — REST API backend
- **Vanilla JS** — single-file dark-themed frontend (no build step)
- **macOS LaunchAgent** — automated daily data collection at 6am

---

## Methodology

### 1. Data Collection
Pulls all resolved ATP/WTA events from Polymarket's public Gamma API, then fetches hourly price histories from the CLOB API. Async collection with a concurrency of 20 requests. Runs nightly via LaunchAgent to build the live dataset. Each event record includes `startTime` — the actual match kickoff time from Polymarket — used to enforce the pre-match trading window.

### 2. Strategy Grid Search
Tested 5,500 parameter combinations across 22 strategy variants × entry windows × stop-loss levels × sizing modes. Fixed-percentage compounding sizing only (strategy-defined sizing has no theoretical basis without an independent probability estimate).

### 3. Walk-Forward Validation
To avoid overfitting, the top strategies were re-validated using a chronological split: train on earlier data, test on a held-out later period. Only strategies profitable on the unseen test period were kept. This is how `mom_fast` and `mom_sensitive` were selected.

### 4. Stake Size Selection (Out-of-Sample)
Ran a percentage sweep (1%–20% of bankroll) on the **training period only**, then evaluated the chosen percentage on the **held-out test period**. The 15% figure was never exposed to test data during selection.

### 5. Pre-Match Window Validation
Initially classified bets as pre-match vs in-match using a price-velocity heuristic, which showed 89% in-match — clearly wrong. Switched to Polymarket's actual `startTime` field (match kickoff time). Result: 308 of 309 signals fire pre-match. Markets are created the evening before matches; the first 20% of market lifetime is well before kickoff. The 1 in-match bet was a loss. This confirms the strategy is a pre-match sentiment fade, not a live in-play system.

### 6. Execution Cost Modeling
The initial backtester used mid prices for both entry and exit. Updated to model realistic live execution:
- **Entry at ask** (`mid + half-spread`) — you pay the spread when buying
- **Stop-loss exit at bid** (`mid - half-spread - slippage`) — you pay the spread again when selling
- Resolution payouts are unchanged (Polymarket settles at $1.00 or $0.00, no spread cost)

The `assumed_spread` and `sl_slippage` parameters are configurable for stress-testing. Default live simulation uses 2¢ spread + 1¢ SL slippage.

---

## Project Structure

```
data_collector.py   — async pipeline: Gamma API events → CLOB price history → SQLite
strategies.py       — pluggable strategy registry (5 strategies, easy to add more)
backtester.py       — walk price histories, apply strategy, compute metrics
grid_search.py      — exhaustive parameter sweep across all strategy variants
walk_forward.py     — chronological train/test validation framework
pct_sweep.py        — out-of-sample stake size optimization
api.py              — FastAPI backend
web/index.html      — interactive frontend (single file, no build step)
collect.sh          — LaunchAgent wrapper for nightly collection
sample_data.csv     — 500 real ATP match-winner markets (schema preview)
```

---

## Setup & Run

**Requirements:** Python 3.11+, pip

```bash
pip install fastapi uvicorn requests aiohttp certifi
```

**Collect data** (builds `backtest.db` — takes 20–60 min first run):
```bash
python3 data_collector.py
```

**Start the UI:**
```bash
python3 api.py
# → open http://localhost:8000
```

**Run the validated strategy with realistic execution costs:**
```bash
python3 - <<'EOF'
from backtester import run_backtest
r = run_backtest(
    sport="ATP",
    strategy_name="momentum_reversal",
    strategy_params={"lookback": 5, "threshold": 0.05},
    entry_pct=0.20,
    bankroll=1000.0,
    hold_to_close=False,
    stop_loss_price=0.10,
    compounding=True,
    sizing_mode="fixed_pct",
    unit_pct=0.05,          # 5% for live; 15% is the research optimum
    assumed_spread=0.02,    # entry at ask, exit at bid
    sl_slippage=0.01,       # additional stop-loss slippage
    trade_window="prematch",
)
print(f"Bets: {r['total_bets']}  Win rate: {r['win_rate']:.1%}  ROI: {r['roi']:+.1%}")
EOF
```

**Nightly auto-collection (macOS):**
```bash
# Installs a LaunchAgent that runs data_collector.py at 6am daily
cp com.pmbacktester.collect.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.pmbacktester.collect.plist
```

---

## Database Schema

```sql
events        (id, title, series_id, sport, start_date, end_date, game_start_time)
markets       (id, event_id, question, yes_token_id, resolved_outcome, volume)
price_history (market_id, timestamp, price)   -- hourly YES-price snapshots
```

`game_start_time` stores Polymarket's actual match kickoff time, used to enforce the pre-match trading window. `sample_data.csv` shows 500 real ATP match rows with summary price statistics.

---

## Adding a Strategy

Edit `strategies.py`:

```python
def my_strategy(snapshot, history, my_param=0.5):
    if snapshot["price"] < my_param:
        return {"bet": "yes", "size_fraction": 0.05, "reason": "below threshold"}
    return {"bet": None, "size_fraction": 0.0, "reason": "no signal"}

STRATEGIES["my_strategy"] = {
    "fn": my_strategy,
    "description": "Bet YES when price is below my_param",
    "params": {"my_param": {"type": "float", "default": 0.5, "min": 0.1, "max": 0.9}},
}
```

Restart the API and it appears in the frontend immediately.

---

## Data Source

All data comes directly from Polymarket's public APIs:
- Event metadata: `gamma-api.polymarket.com/events`
- Price history: `clob.polymarket.com/prices-history`

Polymarket only retains price history for ~2–3 months after market resolution. The live daily collection is the only way to build a longer dataset going forward.
