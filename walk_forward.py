"""
Walk-forward validation to find strategies that hold up out-of-sample.

Splits ATP event history chronologically into folds:
  Train on fold 1       → record best params
  Train on folds 1+2    → record best params
  Test each fold's best params on the NEXT fold (never seen during search)

Reports which strategy/params consistently produce positive ROI
on data they were never tuned on.
"""

import sqlite3
from backtester import run_backtest

SPORT    = "ATP"
BANKROLL = 1000.0
MIN_BETS = 15           # minimum bets in a period to count the result

# ── get chronological date boundaries ─────────────────────────────────────────

def get_fold_dates(n_folds=4):
    """Return date boundaries splitting the USABLE date range (events that have
    price history) into n_folds equal chunks."""
    conn = sqlite3.connect("backtest.db")
    # Only include dates where price history actually exists
    dates = conn.execute(
        """SELECT DISTINCT substr(e.start_date, 1, 10) as dt
           FROM events e
           JOIN markets m ON e.id = m.event_id
           JOIN price_history ph ON m.id = ph.market_id
           WHERE e.sport = 'ATP'
           ORDER BY dt"""
    ).fetchall()
    conn.close()
    dates = [d[0] for d in dates if d[0]]
    if not dates:
        raise RuntimeError("No events with price history found in the database.")
    n = len(dates)
    print(f"Usable date range: {dates[0]} → {dates[-1]}  ({n} distinct dates, {n_folds} folds)")
    if n < n_folds * 7:
        print(f"WARNING: only {n} days of data for {n_folds} folds — results will be noisy")
    boundaries = [dates[0]]
    for i in range(1, n_folds):
        boundaries.append(dates[int(i * n / n_folds)])
    boundaries.append(None)
    return boundaries

# ── parameter grid (flat sizing only) ─────────────────────────────────────────

COMBOS = [
    ("price_threshold",   {"threshold": 0.20, "side": "yes"},  "pt_0.20_yes"),
    ("price_threshold",   {"threshold": 0.25, "side": "yes"},  "pt_0.25_yes"),
    ("price_threshold",   {"threshold": 0.30, "side": "yes"},  "pt_0.30_yes"),
    ("price_threshold",   {"threshold": 0.35, "side": "yes"},  "pt_0.35_yes"),
    ("price_threshold",   {"threshold": 0.40, "side": "yes"},  "pt_0.40_yes"),
    ("price_threshold",   {"threshold": 0.25, "side": "both"}, "pt_0.25_both"),
    ("price_threshold",   {"threshold": 0.30, "side": "both"}, "pt_0.30_both"),
    ("price_threshold",   {"threshold": 0.35, "side": "both"}, "pt_0.35_both"),
    ("momentum_reversal", {"lookback": 5,  "threshold": 0.05}, "mom_fast"),
    ("momentum_reversal", {"lookback": 10, "threshold": 0.05}, "mom_sensitive"),
    ("momentum_reversal", {"lookback": 10, "threshold": 0.08}, "mom_default"),
    ("fade_favorite",     {"min_price": 0.55, "max_price": 0.75}, "fade_55-75"),
    ("fade_favorite",     {"min_price": 0.60, "max_price": 0.80}, "fade_60-80"),
    ("late_drift",        {"entry_window": 0.15, "drift_threshold": 0.05}, "drift_default"),
]

ENTRY_PCTS  = [0.20, 0.35, 0.50]
STOP_LOSSES = [0.10, 0.15, None]

# ── run one backtest, return ROI and bet count ────────────────────────────────

def evaluate(sname, sparams, entry_pct, sl, date_from, date_to):
    r = run_backtest(
        sport=SPORT,
        strategy_name=sname,
        strategy_params=sparams,
        entry_pct=entry_pct,
        bankroll=BANKROLL,
        hold_to_close=(sl is None),
        stop_loss_price=sl,
        compounding=False,          # flat ROI for fair comparison across periods
        sizing_mode="fixed_dollar",
        unit_size=50.0,
        date_from=date_from,
        date_to=date_to,
    )
    return r["roi"], r["total_bets"], r["win_rate"]

# ── walk-forward ──────────────────────────────────────────────────────────────

def walk_forward(n_folds=4):
    boundaries = get_fold_dates(n_folds)
    print(f"Date boundaries: {boundaries}\n")

    fold_results = []   # per-fold out-of-sample results keyed by combo label

    # Walk: train on folds 0..k-1, test on fold k
    for test_fold in range(1, n_folds):
        train_from = boundaries[0]
        train_to   = boundaries[test_fold]
        test_from  = boundaries[test_fold]
        test_to    = boundaries[test_fold + 1]

        print(f"{'='*70}")
        print(f"Train: {train_from} → {train_to}   |   Test: {test_from} → {test_to}")
        print(f"{'='*70}")

        # ── find best params on training period ───────────────────────────────
        train_best = []
        for sname, sparams, label in COMBOS:
            for entry_pct in ENTRY_PCTS:
                for sl in STOP_LOSSES:
                    roi, bets, wr = evaluate(sname, sparams, entry_pct, sl,
                                             train_from, train_to)
                    if bets >= MIN_BETS:
                        train_best.append((roi, bets, wr, label, sname, sparams,
                                           entry_pct, sl))

        train_best = [x for x in train_best if x[0] is not None]
        train_best.sort(key=lambda x: x[0], reverse=True)
        top_n = train_best[:5]

        print(f"  Top 5 on TRAIN data:")
        for roi, bets, wr, label, sname, sparams, entry_pct, sl in top_n:
            sl_s = f"{sl:.2f}" if sl else "None"
            print(f"    {label:<20} entry={entry_pct:.0%} sl={sl_s}  "
                  f"train_roi={roi:+.1%}  bets={bets}  wr={wr:.1%}")

        # ── evaluate those same params on the test period ─────────────────────
        print(f"\n  Out-of-sample TEST results:")
        fold_oos = []
        for _, _, _, label, sname, sparams, entry_pct, sl in top_n:
            oos_roi, oos_bets, oos_wr = evaluate(sname, sparams, entry_pct, sl,
                                                  test_from, test_to)
            sl_s = f"{sl:.2f}" if sl else "None"
            flag = "✓" if oos_roi > 0 and oos_bets >= MIN_BETS else "✗"
            print(f"    {flag} {label:<20} entry={entry_pct:.0%} sl={sl_s}  "
                  f"oos_roi={oos_roi:+.1%}  bets={oos_bets}  wr={oos_wr:.1%}")
            fold_oos.append({
                "label": label, "sname": sname, "sparams": sparams,
                "entry_pct": entry_pct, "sl": sl,
                "oos_roi": oos_roi, "oos_bets": oos_bets,
            })
        fold_results.append(fold_oos)

    # ── summary: which combos were consistently positive out-of-sample ────────
    print(f"\n{'='*70}")
    print("CONSISTENCY SUMMARY — strategies positive OOS across all test folds")
    print(f"{'='*70}")

    # collect unique combos that appeared in any fold's top-5
    seen = {}
    for fold in fold_results:
        for r in fold:
            key = (r["label"], r["entry_pct"], r["sl"])
            if key not in seen:
                seen[key] = []
            seen[key].append(r["oos_roi"])

    consistent = []
    for key, rois in seen.items():
        avg = sum(rois) / len(rois)
        positive = sum(1 for r in rois if r > 0)
        consistent.append((positive, len(rois), avg, key, rois))

    consistent.sort(key=lambda x: (-x[0], -x[2]))

    print(f"{'Combo':<42} {'OOS Folds':>10} {'Avg OOS ROI':>12}  Per-fold ROIs")
    print("-" * 80)
    for pos, total, avg, (label, entry_pct, sl), rois in consistent:
        sl_s = f"{sl:.2f}" if sl else "None"
        roi_str = "  ".join(f"{r:+.1%}" for r in rois)
        print(f"{label:<20} entry={entry_pct:.0%} sl={sl_s}  "
              f"  {pos}/{total} positive  avg={avg:+.1%}   [{roi_str}]")

if __name__ == "__main__":
    walk_forward(n_folds=4)
