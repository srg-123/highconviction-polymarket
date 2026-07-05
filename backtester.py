"""
Backtester: walks resolved market price histories, applies strategies,
computes calibration tables and performance metrics.
"""

import os
import sqlite3
from collections import defaultdict
from typing import Optional

DB_PATH = os.environ.get("DB_PATH", "backtest.db")

SPORT_SERIES: dict[str, int] = {
    "ATP": 10365,
    "WTA": 10366,
    "ITF": 11634,
    "MLB": 3,
    "UFC": 38,
}


# ── DB helpers ────────────────────────────────────────────────────────────────

# Excludes prop markets (set/game O/U, set winners, total sets, etc.) so only
# the main moneyline ("X vs Y") market per event remains.
_MATCH_WINNER_FILTER = """
    AND m.question LIKE '%vs%'
    AND m.question NOT LIKE '%O/U%'
    AND m.question NOT LIKE '%Set %'
    AND m.question NOT LIKE '%Completed Match%'
    AND m.question NOT LIKE '%Total%'
"""


def _get_markets(conn: sqlite3.Connection, sport: Optional[str],
                 date_from: Optional[str] = None,
                 date_to:   Optional[str] = None) -> list:
    date_filter = ""
    if date_from:
        date_filter += f" AND e.start_date >= '{date_from}'"
    if date_to:
        date_filter += f" AND e.start_date < '{date_to}'"

    select = """SELECT m.id, m.question, m.resolved_outcome, m.volume,
                       e.sport, e.title, e.game_start_time
               FROM markets m
               JOIN events e ON m.event_id = e.id
               WHERE m.resolved_outcome IS NOT NULL"""
    if sport and sport.upper() != "ALL":
        sid = SPORT_SERIES.get(sport.upper())
        if not sid:
            return []
        rows = conn.execute(
            select + " AND e.series_id = ?" + _MATCH_WINNER_FILTER + date_filter,
            (sid,),
        ).fetchall()
    else:
        rows = conn.execute(select + _MATCH_WINNER_FILTER + date_filter).fetchall()
    return rows


def _get_price_history(conn: sqlite3.Connection, market_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT timestamp, price FROM price_history WHERE market_id=? ORDER BY timestamp",
        (market_id,),
    ).fetchall()
    return [{"timestamp": r[0], "price": r[1]} for r in rows]


# ── calibration ───────────────────────────────────────────────────────────────

def calibration_only(sport: Optional[str] = None) -> list[dict]:
    """
    Pure calibration analysis — no strategy.
    Groups price observations into 10-pt buckets and measures actual win rate.
    Returns list of {bucket, count, market_prob, actual_win_rate, edge}.
    """
    conn = sqlite3.connect(DB_PATH)
    markets = _get_markets(conn, sport)

    buckets: dict[str, dict] = defaultdict(lambda: {"won": 0, "total": 0, "sum_price": 0.0})

    for market_id, _, resolved_outcome, _, _, _ in markets:
        history = _get_price_history(conn, market_id)
        n = len(history)
        if n < 4:
            continue

        # Sample at 25 %, 50 %, 75 % through the market's recorded life
        for idx in (n // 4, n // 2, 3 * n // 4):
            p = history[idx]["price"]
            low = int(p * 10) * 10
            label = f"{low}-{low+10}%"
            b = buckets[label]
            b["total"] += 1
            b["sum_price"] += p
            if resolved_outcome == 1:
                b["won"] += 1

    conn.close()

    result = []
    for label in sorted(buckets, key=lambda x: int(x.split("-")[0])):
        b = buckets[label]
        if b["total"] == 0:
            continue
        win_rate  = b["won"] / b["total"]
        avg_price = b["sum_price"] / b["total"]
        result.append({
            "bucket":          label,
            "count":           b["total"],
            "market_prob":     round(avg_price, 3),
            "actual_win_rate": round(win_rate, 3),
            "edge":            round(win_rate - avg_price, 3),
        })
    return result


# ── backtest engine ───────────────────────────────────────────────────────────

def _pnl_resolution(bet: str, entry_price: float, stake: float, won: bool) -> float:
    if bet == "yes":
        return stake * (1.0 / entry_price - 1.0) if won else -stake
    else:
        no_price = 1.0 - entry_price
        return stake * (1.0 / no_price - 1.0) if won else -stake


def _pnl_exit(bet: str, entry_price: float, exit_price: float, stake: float) -> float:
    """Mark-to-market P&L from selling the position at exit_price before resolution."""
    if bet == "yes":
        return stake * (exit_price / entry_price - 1.0)
    else:
        entry_no = 1.0 - entry_price
        exit_no  = 1.0 - exit_price
        return stake * (exit_no / entry_no - 1.0)


def _scan_fill(history: list, start: int, end: int, target_price: float,
               bet_side: str, is_buy: bool) -> Optional[int]:
    """
    Scan history[start:end+1] (YES prices) for the first index where a resting
    limit order at target_price would fill.

    A "yes" buy / "no" sell fills when price <= target_price (the YES price
    has come down to or below your limit). A "yes" sell / "no" buy fills when
    price >= target_price. Returns the index, or None if never filled.
    """
    wants_low = (bet_side == "yes") == is_buy
    for j in range(start, end + 1):
        p = history[j]["price"]
        if (p <= target_price) if wants_low else (p >= target_price):
            return j
    return None


def _scan_cross(history: list, start: int, end: int, target_price: float, direction: str) -> Optional[int]:
    """Scan history[start:end+1] for the first index where price reaches
    target_price. direction='up' looks for price >= target, 'down' for
    price <= target. Returns the index, or None if never reached."""
    for j in range(start, end + 1):
        p = history[j]["price"]
        if (p >= target_price) if direction == "up" else (p <= target_price):
            return j
    return None


def run_backtest(
    sport: Optional[str] = None,
    strategy_name: str = "price_threshold",
    strategy_params: Optional[dict] = None,
    entry_pct: float = 0.5,
    bankroll: float = 1000.0,
    hold_to_close: bool = True,
    take_profit_price: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
    sl_slippage: float = 0.0,
    assumed_spread: float = 0.0,
    compounding: bool = False,
    sizing_mode: str = "strategy",
    unit_size: float = 50.0,
    unit_pct: float = 0.05,
    limit_orders: bool = False,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    trade_window: str = "all",
) -> dict:
    # trade_window: "all" | "prematch" | "inmatch"
    # prematch = only enter before game_start_time; inmatch = only enter after
    """
    Walk price histories, apply strategy, return aggregated metrics + sample bets.

    entry_pct     — only consider entry signals up to this fraction of market life.
    hold_to_close — if True, hold every position until resolution (P&L from
                    final outcome). If False, place resting take-profit and/or
                    stop-loss limit orders right after entry; the position
                    exits the moment price trades to (or through) whichever
                    level is hit first. If neither is ever reached before
                    resolution, the position falls back to holding to close.
    take_profit_price — YES-price level for the profitable exit, when
                    hold_to_close is False. For "yes" bets this should be
                    above the entry price (exit fills once price rises to
                    this level); for "no" bets it should be below the entry
                    price (exit fills once price falls to this level). Set to
                    None to disable.
    stop_loss_price — YES-price level for the loss-cutting exit, when
                    hold_to_close is False. For "yes" bets this should be
                    below the entry price (exit fills once price falls to
                    this level); for "no" bets it should be above the entry
                    price (exit fills once price rises to this level). Set to
                    None to disable.
    bankroll      — starting notional bankroll for $ P&L calculation.
    compounding   — if True, "strategy" sizing is based off the running
                    bankroll (bets sorted chronologically by entry time), so
                    wins/losses compound. If False (default), every bet sizes
                    off the fixed starting bankroll. Either way the running
                    bankroll is tracked for reporting.
    sizing_mode   — "strategy" (default): stake = strategy's size_fraction *
                    bankroll. "fixed_dollar": every bet stakes a flat
                    `unit_size` dollar amount. "fixed_pct": every bet stakes
                    `unit_pct` of the (running, if compounding) bankroll.
                    Both fixed modes ignore the strategy's size_fraction.
    unit_size     — flat dollar stake per bet when sizing_mode == "fixed_dollar".
    unit_pct      — fraction of bankroll staked per bet when
                    sizing_mode == "fixed_pct" (e.g. 0.05 = 5%).
    limit_orders  — if True, entry is also simulated as a resting limit order:
                    a buy limit at the signal price is placed one tick later
                    and only fills if price subsequently trades back to (or
                    through) that level within the entry window; otherwise
                    that signal is skipped and the next one is tried.
    """
    if sizing_mode not in ("strategy", "fixed_dollar", "fixed_pct"):
        raise ValueError(f"Unknown sizing_mode: {sizing_mode}")
    from strategies import STRATEGIES

    if strategy_name not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {strategy_name}")

    strategy_fn = STRATEGIES[strategy_name]["fn"]
    params = strategy_params or {}

    conn = sqlite3.connect(DB_PATH)
    markets = _get_markets(conn, sport, date_from=date_from, date_to=date_to)

    raw_bets = []
    cal_dict: Optional[dict] = None

    if strategy_name == "calibration_bucket":
        cal_rows = calibration_only(sport)
        cal_dict = {r["bucket"]: r for r in cal_rows}

    for market_id, question, resolved_outcome, volume, mkt_sport, event_title, game_start_time in markets:
        history = _get_price_history(conn, market_id)
        n = len(history)
        if n < 5:
            continue

        # Resolve game_start_time to a unix timestamp for boundary checks
        gst_ts: Optional[int] = None
        if game_start_time:
            import calendar, email.utils
            try:
                from datetime import datetime, timezone
                gst_ts = int(datetime.fromisoformat(
                    game_start_time.replace("Z", "+00:00")
                ).timestamp())
            except Exception:
                gst_ts = None

        cutoff = max(2, int(n * entry_pct))

        for i in range(2, cutoff):
            snapshot = history[i]
            past     = history[:i]

            # Enforce pre/in-match window using game_start_time
            if gst_ts is not None and trade_window != "all":
                ts = snapshot["timestamp"]
                if trade_window == "prematch" and ts >= gst_ts:
                    break   # past match start — no more pre-match signals possible
                if trade_window == "inmatch" and ts < gst_ts:
                    continue  # before match start — skip

            if strategy_name == "calibration_bucket":
                signal = strategy_fn(snapshot, past, calibration=cal_dict, **params)
            else:
                signal = strategy_fn(snapshot, past, **params)

            if signal["bet"] is None:
                continue

            bet_side    = signal["bet"]
            size_frac   = signal["size_fraction"]
            # Entry at ask (mid + half-spread) for YES buys,
            # bid (mid - half-spread) for NO buys.
            half_spread = assumed_spread / 2.0
            mid_price   = snapshot["price"]
            if bet_side == "yes":
                entry_price = min(0.99, mid_price + half_spread)
            else:
                entry_price = max(0.01, mid_price - half_spread)
            entry_idx   = i
            entry_timestamp = snapshot["timestamp"]

            if limit_orders:
                fill_idx = _scan_fill(history, i + 1, cutoff - 1, entry_price,
                                       bet_side, is_buy=True)
                if fill_idx is None:
                    continue  # limit buy never filled within entry window — try next signal
                entry_idx = fill_idx
                entry_timestamp = history[fill_idx]["timestamp"]

            if hold_to_close:
                won = (
                    (bet_side == "yes" and resolved_outcome == 1) or
                    (bet_side == "no"  and resolved_outcome == 0)
                )
                exit_price = None
                exit_timestamp = None
            else:
                # Resting take-profit / stop-loss limit orders placed right after entry.
                # For "yes" bets, take-profit fires on a price rise, stop-loss on a fall.
                # For "no" bets (profit as YES price falls), it's reversed.
                tp_dir, sl_dir = ("up", "down") if bet_side == "yes" else ("down", "up")

                # Only scan TP if the level is on the profitable side of entry,
                # and SL only if it's on the loss side. A TP/SL on the wrong
                # side of entry makes no sense and would fire spuriously.
                tp_valid = (
                    take_profit_price is not None and
                    ((bet_side == "yes" and take_profit_price > entry_price) or
                     (bet_side == "no"  and take_profit_price < entry_price))
                )
                sl_valid = (
                    stop_loss_price is not None and
                    ((bet_side == "yes" and stop_loss_price < entry_price) or
                     (bet_side == "no"  and stop_loss_price > entry_price))
                )

                tp_idx = _scan_cross(history, entry_idx + 1, n - 1, take_profit_price, tp_dir) \
                    if tp_valid else None
                sl_idx = _scan_cross(history, entry_idx + 1, n - 1, stop_loss_price, sl_dir) \
                    if sl_valid else None

                candidates = [c for c in ((tp_idx, "tp"), (sl_idx, "sl"))
                               if c[0] is not None]

                if not candidates:
                    # neither level reached — fall back to hold-to-close
                    won = (
                        (bet_side == "yes" and resolved_outcome == 1) or
                        (bet_side == "no"  and resolved_outcome == 0)
                    )
                    exit_price = None
                    exit_timestamp = None
                else:
                    fill_idx, exit_kind = min(candidates, key=lambda c: c[0])
                    exit_timestamp = history[fill_idx]["timestamp"]
                    # Use actual market price at the exit tick (captures gapping).
                    # For stop-loss exits also subtract any additional slippage.
                    actual_price = history[fill_idx]["price"]
                    if exit_kind == "sl":
                        # Exit via marketable limit sell at bid - slippage.
                        # bid = mid - half_spread; subtract additional slippage on top.
                        if bet_side == "yes":
                            exit_price = max(0.01, actual_price - half_spread - sl_slippage)
                        else:
                            exit_price = min(0.99, actual_price + half_spread + sl_slippage)
                    else:
                        # TP exit: selling into the bid side (paying spread to exit)
                        if bet_side == "yes":
                            exit_price = max(0.01, actual_price - half_spread)
                        else:
                            exit_price = min(0.99, actual_price + half_spread)
                    ratio = (exit_price / entry_price) if bet_side == "yes" \
                        else (1.0 - exit_price) / (1.0 - entry_price)
                    won = ratio > 1.0

            raw_bets.append({
                "market_id":       market_id,
                "question":        question,
                "sport":           mkt_sport,
                "event_title":     event_title,
                "bet":             bet_side,
                "entry_price":     entry_price,
                "exit_price":      exit_price,
                "size_fraction":   size_frac,
                "resolved_outcome": resolved_outcome,
                "won":             won,
                "reason":          signal["reason"],
                "timestamp":       entry_timestamp,
                "exit_timestamp":  exit_timestamp,
            })
            break  # one bet per market

    conn.close()

    if not raw_bets:
        return {
            "bets": [], "total_bets": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "total_wagered": 0.0, "total_pnl": 0.0,
            "roi": 0.0, "strategy": strategy_name, "sport": sport or "all",
            "compounding": compounding,
            "starting_bankroll": bankroll, "final_bankroll": bankroll,
        }

    # Chronological order matters for compounding (each bet sizes off the
    # bankroll resulting from all prior bets).
    raw_bets.sort(key=lambda b: b["timestamp"])

    bets = []
    running_bankroll = bankroll
    for rb in raw_bets:
        current_bankroll = running_bankroll if compounding else bankroll

        if sizing_mode == "fixed_dollar":
            stake = unit_size
        elif sizing_mode == "fixed_pct":
            stake = unit_pct * current_bankroll
        else:
            stake = rb["size_fraction"] * current_bankroll

        if rb["exit_price"] is None:
            pnl = _pnl_resolution(rb["bet"], rb["entry_price"], stake, rb["won"])
        else:
            pnl = _pnl_exit(rb["bet"], rb["entry_price"], rb["exit_price"], stake)

        if compounding:
            running_bankroll += pnl

        bets.append({
            **{k: v for k, v in rb.items() if k not in ("entry_price", "exit_price", "size_fraction")},
            "entry_price":   round(rb["entry_price"], 3),
            "exit_price":    round(rb["exit_price"], 3) if rb["exit_price"] is not None else None,
            "size_fraction": round(rb["size_fraction"], 4),
            "stake":         round(stake, 2),
            "pnl":           round(pnl, 2),
            "bankroll_after": round(running_bankroll, 2) if compounding else None,
        })

    wins          = sum(1 for b in bets if b["won"])
    total_wagered = sum(b["stake"] for b in bets)
    total_pnl     = sum(b["pnl"] for b in bets)
    roi           = total_pnl / total_wagered if total_wagered > 0 else 0.0

    return {
        "bets":              bets[:100],
        "total_bets":        len(bets),
        "wins":              wins,
        "losses":            len(bets) - wins,
        "win_rate":          round(wins / len(bets), 4),
        "total_wagered":     round(total_wagered, 2),
        "total_pnl":         round(total_pnl, 2),
        "roi":               round(roi, 4),
        "strategy":          strategy_name,
        "sport":             sport or "all",
        "compounding":       compounding,
        "starting_bankroll": bankroll,
        "final_bankroll":    round(running_bankroll, 2) if compounding else None,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== CALIBRATION (ATP) ===")
    for row in calibration_only("ATP"):
        bar = "█" * int(abs(row["edge"]) * 200)
        sign = "+" if row["edge"] >= 0 else ""
        print(f"  {row['bucket']:12s}  n={row['count']:5d}  "
              f"mkt={row['market_prob']:.3f}  actual={row['actual_win_rate']:.3f}  "
              f"edge={sign}{row['edge']:.3f}  {bar}")

    print("\n=== BACKTEST: price_threshold / ATP ===")
    r = run_backtest("ATP", "price_threshold", {"threshold": 0.35})
    print(f"  Bets={r['total_bets']}  WR={r['win_rate']:.1%}  "
          f"ROI={r['roi']:.2%}  P&L=${r['total_pnl']:.0f}")
