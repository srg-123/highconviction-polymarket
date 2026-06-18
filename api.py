"""
FastAPI backend.  Serves the frontend from web/index.html.

Endpoints:
  GET /            — frontend HTML
  GET /stats       — DB summary (event/market/price-point counts by sport)
  GET /sports      — list of supported sports
  GET /strategies  — list of strategies + param schemas
  GET /calibration — calibration table (?sport=ATP)
  GET /backtest    — run backtest (?sport=ATP&strategy=price_threshold&...)
"""

import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from backtester import SPORT_SERIES, calibration_only, run_backtest
from strategies import STRATEGIES

app = FastAPI(title="Polymarket Backtester", version="1.0.0")

DB_PATH = "backtest.db"


def _db():
    return sqlite3.connect(DB_PATH)


def _db_exists() -> bool:
    return Path(DB_PATH).exists()


# ── frontend ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_frontend():
    p = Path("web/index.html")
    if p.exists():
        return HTMLResponse(content=p.read_text())
    return HTMLResponse("<h1>Frontend not built</h1><p>web/index.html missing</p>", status_code=500)


# ── stats ─────────────────────────────────────────────────────────────────────

@app.get("/stats")
def stats():
    if not _db_exists():
        return {"total_events": 0, "resolved_markets": 0,
                "price_data_points": 0, "by_sport": [],
                "note": "Run data_collector.py first"}

    conn = _db()
    total_events   = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    resolved_mkts  = conn.execute(
        "SELECT COUNT(*) FROM markets WHERE resolved_outcome IS NOT NULL"
    ).fetchone()[0]
    price_pts      = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    by_sport       = conn.execute(
        """SELECT e.sport,
                  COUNT(DISTINCT e.id) AS events,
                  COUNT(DISTINCT CASE WHEN m.resolved_outcome IS NOT NULL THEN m.id END) AS markets
           FROM events e
           LEFT JOIN markets m ON e.id = m.event_id
           GROUP BY e.sport
           ORDER BY e.sport"""
    ).fetchall()
    conn.close()

    return {
        "total_events":      total_events,
        "resolved_markets":  resolved_mkts,
        "price_data_points": price_pts,
        "by_sport": [
            {"sport": r[0], "events": r[1], "markets": r[2]}
            for r in by_sport
        ],
    }


# ── sports ────────────────────────────────────────────────────────────────────

@app.get("/sports")
def sports():
    return [{"key": k, "series_id": v} for k, v in SPORT_SERIES.items()]


# ── strategies ────────────────────────────────────────────────────────────────

@app.get("/strategies")
def strategies():
    return [
        {"name": name, "description": info["description"], "params": info["params"]}
        for name, info in STRATEGIES.items()
    ]


# ── calibration ───────────────────────────────────────────────────────────────

@app.get("/calibration")
def calibration(sport: Optional[str] = Query(default=None)):
    if not _db_exists():
        raise HTTPException(503, "Database not found — run data_collector.py first")
    return calibration_only(sport)


# ── backtest ──────────────────────────────────────────────────────────────────

@app.get("/backtest")
def backtest(
    sport:       Optional[str]   = Query(default=None),
    strategy:    str             = Query(default="price_threshold"),
    entry_pct:   float           = Query(default=0.5,  ge=0.05, le=0.95),
    bankroll:    float           = Query(default=1000.0, ge=100.0),
    hold_to_close: bool          = Query(default=True),
    take_profit_price: Optional[float] = Query(default=None, ge=0.01, le=0.99),
    stop_loss_price:   Optional[float] = Query(default=None, ge=0.01, le=0.99),
    sl_slippage:       float           = Query(default=0.0, ge=0.0, le=0.20),
    compounding: bool            = Query(default=False),
    sizing_mode: str             = Query(default="strategy"),
    unit_size:   float           = Query(default=50.0, gt=0),
    unit_pct:    float           = Query(default=0.05, gt=0, le=1.0),
    limit_orders: bool           = Query(default=False),
    # strategy-specific knobs (forwarded as params)
    threshold:   Optional[float] = Query(default=None),
    side:        Optional[str]   = Query(default=None),
    lookback:    Optional[int]   = Query(default=None),
    min_edge:    Optional[float] = Query(default=None),
    min_price:   Optional[float] = Query(default=None),
    max_price:   Optional[float] = Query(default=None),
    entry_window:    Optional[float] = Query(default=None),
    drift_threshold: Optional[float] = Query(default=None),
):
    if not _db_exists():
        raise HTTPException(503, "Database not found — run data_collector.py first")

    params: dict = {}
    for key, val in {
        "threshold": threshold, "side": side, "lookback": lookback,
        "min_edge": min_edge, "min_price": min_price, "max_price": max_price,
        "entry_window": entry_window, "drift_threshold": drift_threshold,
    }.items():
        if val is not None:
            params[key] = val

    try:
        return run_backtest(
            sport=sport,
            strategy_name=strategy,
            strategy_params=params,
            entry_pct=entry_pct,
            bankroll=bankroll,
            hold_to_close=hold_to_close,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            sl_slippage=sl_slippage,
            compounding=compounding,
            sizing_mode=sizing_mode,
            unit_size=unit_size,
            unit_pct=unit_pct,
            limit_orders=limit_orders,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ── dev entry ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
