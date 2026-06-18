# Polymarket Tennis Backtester

A data pipeline and backtesting framework for discovering and validating profitable trading strategies on [Polymarket](https://polymarket.com) tennis prediction markets (ATP & WTA).

Built entirely from scratch — data collection, strategy engine, walk-forward validation, and an interactive web UI.

---

## Key Results

The best strategy found — **momentum reversal on ATP underdogs** — was validated using walk-forward analysis (train on early data, test on unseen later data):

| Period | Bets | Win Rate | ROI |
|--------|------|----------|-----|
| Training (Apr 11 – Jun 9) | 273 | 41.0% | +10.4% |
| **Out-of-sample test (Jun 9 – present)** | **36** | **52.8%** | **+29.5%** |

The out-of-sample win rate (52.8%) is *higher* than training — on markets priced at ≤20% to win. That gap between implied probability and actual win rate is the edge the strategy is exploiting.

Slippage stress-testing confirmed the edge survives realistic exit execution (actual fill price, not just the trigger):

| Exit model | ROI |
|------------|-----|
| Trigger price (optimistic) | ~+22% |
| Actual market price at trigger | +13.8% |
| Actual price + 2¢ slippage | +12.2% |

---

## What the Strategy Does

**`momentum_reversal` — mom_fast variant**

Every hour, Polymarket records a price for each player's win contract. This strategy:

1. Looks at a player's price now vs 5 hours ago
2. If it dropped 5+ percentage points (market overreacted against the underdog)
3. **And** the player is priced at 20% or below (clear underdog, overreaction is largest here)
4. Bets YES — expecting the price to drift back up

Stop-loss at 10¢: if the market disagrees further, exit rather than hold to zero.

Optimal stake: **15% of bankroll per bet**, compounding. Found on training data only — not tuned on the test period.

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
Pulls all resolved ATP/WTA events from Polymarket's public Gamma API, then fetches hourly price histories from the CLOB API. Async collection with a concurrency of 20 requests. Runs nightly via LaunchAgent to build the live dataset.

### 2. Strategy Grid Search
Tested 5,500 parameter combinations across 22 strategy variants × entry prices × take-profit/stop-loss levels × sizing modes. Compounding, fixed-percentage sizing only (removed strategy-defined sizing — it has no theoretical basis without an independent probability estimate).

### 3. Walk-Forward Validation
To avoid overfitting, the top grid-search strategies were re-validated using chronological folds: train on earlier data, test on a later unseen period. Only strategies that were profitable across all test folds were kept. This is how `mom_fast` and `mom_sensitive` were selected.

### 4. Stake Size Selection
Ran a percentage sweep (1%–20% of bankroll) on the **training period only**, then evaluated the chosen percentage on the **held-out test period**. The 15% figure was never exposed to test data during selection.

### 5. Slippage Testing
The initial stop-loss model assumed exact fills at the trigger price. Updated to use the actual recorded market price at the exit tick (which can gap lower in thin markets). Added a configurable `sl_slippage` parameter for additional stress-testing.

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

**Run the validated strategy:**
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
    unit_pct=0.15,
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
events        (id, title, series_id, sport, start_date, end_date)
markets       (id, event_id, question, yes_token_id, resolved_outcome, volume)
price_history (market_id, timestamp, price)   -- hourly YES-price snapshots
```

`sample_data.csv` shows 500 real ATP match rows with summary price statistics.

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
