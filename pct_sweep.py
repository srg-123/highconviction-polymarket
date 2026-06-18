"""
Out-of-sample pct sweep for validated strategies.

Step 1: Sweep unit_pct 1%-20% on the TRAIN period only → find optimal %
Step 2: Run that optimal % on the TEST period → out-of-sample validation
Step 3: Also show the full-dataset curve for reference (but don't use it to pick %)

Split: chronological, same boundaries as walk_forward.py
  Train: 2026-04-11 → 2026-06-05  (~80% of data)
  Test:  2026-06-05 → end          (~20% of data, never touched during search)
"""

import sqlite3
from backtester import run_backtest

SPORT    = "ATP"
BANKROLL = 1000.0
MIN_BETS = 10

# ── validated strategies from walk-forward ────────────────────────────────────

STRATEGIES = [
    ("momentum_reversal", {"lookback": 5,  "threshold": 0.05}, "mom_fast",      0.20, None, 0.10),
    ("momentum_reversal", {"lookback": 10, "threshold": 0.05}, "mom_sensitive", 0.20, None, 0.10),
    ("momentum_reversal", {"lookback": 10, "threshold": 0.08}, "mom_default",   0.20, None, 0.10),
]

UNIT_PCTS = [p / 100 for p in range(1, 21)]

# ── get train/test boundaries from actual data ─────────────────────────────────

def get_split():
    conn = sqlite3.connect("backtest.db")
    dates = conn.execute("""
        SELECT DISTINCT substr(e.start_date, 1, 10) as dt
        FROM events e
        JOIN markets m ON e.id = m.event_id
        JOIN price_history ph ON m.id = ph.market_id
        WHERE e.sport = 'ATP'
        ORDER BY dt
    """).fetchall()
    conn.close()
    dates = [d[0] for d in dates if d[0]]
    # 80/20 chronological split
    split_idx = int(len(dates) * 0.80)
    train_from = dates[0]
    train_to   = dates[split_idx]
    test_from  = dates[split_idx]
    test_to    = None
    return train_from, train_to, test_from, test_to

# ── run one backtest ──────────────────────────────────────────────────────────

def run(sname, sparams, entry_pct, sl, pct, date_from=None, date_to=None):
    r = run_backtest(
        sport=SPORT,
        strategy_name=sname,
        strategy_params=sparams,
        entry_pct=entry_pct,
        bankroll=BANKROLL,
        hold_to_close=(sl is None),
        stop_loss_price=sl,
        compounding=True,
        sizing_mode="fixed_pct",
        unit_pct=pct,
        date_from=date_from,
        date_to=date_to,
    )
    return r["roi"], r["total_bets"], r["win_rate"], r["final_bankroll"]

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    train_from, train_to, test_from, test_to = get_split()
    print(f"Train: {train_from} → {train_to}")
    print(f"Test:  {test_from} → end\n")

    for sname, sparams, label, entry_pct, tp, sl in STRATEGIES:
        print(f"{'='*70}")
        print(f"{label}  |  entry={entry_pct:.0%}  sl={sl}")
        print(f"{'='*70}")

        # ── sweep on train period ─────────────────────────────────────────────
        print(f"\n  TRAIN sweep (1%–20%):")
        print(f"  {'Pct':>5}  {'Bets':>5}  {'WinRate':>8}  {'ROI':>8}  {'FinalBankroll':>14}")
        print(f"  {'-'*50}")

        train_results = []
        for pct in UNIT_PCTS:
            roi, bets, wr, fb = run(sname, sparams, entry_pct, sl, pct,
                                    date_from=train_from, date_to=train_to)
            fb = fb or BANKROLL
            train_results.append((pct, roi, bets, wr, fb))
            print(f"  {pct:>4.0%}   {bets:>5}  {wr:>7.1%}  {roi:>+7.1%}  ${fb:>12,.0f}")

        # find optimal on train (by final bankroll)
        valid = [(p, r, b, w, f) for p, r, b, w, f in train_results if b >= MIN_BETS]
        if not valid:
            print(f"\n  Not enough bets in train period — skipping")
            continue

        best_train = max(valid, key=lambda x: x[4])
        opt_pct = best_train[0]
        print(f"\n  → Optimal on TRAIN: {opt_pct:.0%}  "
              f"(${best_train[4]:,.0f} final, {best_train[1]:+.1%} ROI, {best_train[2]} bets)")

        # ── validate on test period ───────────────────────────────────────────
        print(f"\n  TEST validation at {opt_pct:.0%} (out-of-sample):")
        oos_roi, oos_bets, oos_wr, oos_fb = run(sname, sparams, entry_pct, sl,
                                                  opt_pct,
                                                  date_from=test_from, date_to=test_to)
        oos_fb = oos_fb or BANKROLL
        if oos_bets < MIN_BETS:
            flag = f"⚠ FEW BETS ({oos_bets})"
        elif oos_roi > 0:
            flag = "✓ POSITIVE"
        else:
            flag = "✗ NEGATIVE"
        print(f"  {flag}  ROI={oos_roi:+.1%}  bets={oos_bets}  "
              f"wr={oos_wr:.1%}  final=${oos_fb:,.0f}")

        # also show the test curve at all pcts for context
        print(f"\n  TEST curve (all pcts, for reference only — not used to pick %):")
        print(f"  {'Pct':>5}  {'Bets':>5}  {'WinRate':>8}  {'ROI':>8}  {'FinalBankroll':>14}")
        print(f"  {'-'*50}")
        for pct in UNIT_PCTS:
            roi, bets, wr, fb = run(sname, sparams, entry_pct, sl, pct,
                                    date_from=test_from, date_to=test_to)
            fb = fb or BANKROLL
            marker = " ← optimal from train" if pct == opt_pct else ""
            print(f"  {pct:>4.0%}   {bets:>5}  {wr:>7.1%}  {roi:>+7.1%}  ${fb:>12,.0f}{marker}")

        print()

if __name__ == "__main__":
    main()
