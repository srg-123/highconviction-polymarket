"""
Grid search: find best ROI across every combination of
  - strategy (+ key param variants)
  - entry_pct
  - take_profit_price / stop_loss_price
  - sizing_mode (strategy-defined vs fixed_dollar vs fixed_pct)

ROI = total_pnl / total_wagered, filtered to runs with >= MIN_BETS bets.
For fixed sizing, unit_size/unit_pct cancel out of ROI, so we only run one
representative value per mode.
"""

import itertools
import sys
from backtester import run_backtest

SPORT     = "ATP"
MIN_BETS  = 20
BANKROLL  = 1000.0

# ── parameter grid ────────────────────────────────────────────────────────────

# (strategy_name, strategy_params_dict, label)
STRATEGIES = [
    # price_threshold — YES side only (bet underdogs)
    ("price_threshold",    {"threshold": 0.20, "side": "yes"},  "pt_0.20_yes"),
    ("price_threshold",    {"threshold": 0.25, "side": "yes"},  "pt_0.25_yes"),
    ("price_threshold",    {"threshold": 0.30, "side": "yes"},  "pt_0.30_yes"),
    ("price_threshold",    {"threshold": 0.35, "side": "yes"},  "pt_0.35_yes"),
    ("price_threshold",    {"threshold": 0.40, "side": "yes"},  "pt_0.40_yes"),
    ("price_threshold",    {"threshold": 0.45, "side": "yes"},  "pt_0.45_yes"),
    # price_threshold — both sides (underdogs on either side)
    ("price_threshold",    {"threshold": 0.25, "side": "both"}, "pt_0.25_both"),
    ("price_threshold",    {"threshold": 0.30, "side": "both"}, "pt_0.30_both"),
    ("price_threshold",    {"threshold": 0.35, "side": "both"}, "pt_0.35_both"),
    ("price_threshold",    {"threshold": 0.40, "side": "both"}, "pt_0.40_both"),
    # momentum reversal
    ("momentum_reversal",  {"lookback": 5,  "threshold": 0.05}, "mom_fast"),
    ("momentum_reversal",  {"lookback": 10, "threshold": 0.08}, "mom_default"),
    ("momentum_reversal",  {"lookback": 20, "threshold": 0.10}, "mom_slow"),
    ("momentum_reversal",  {"lookback": 10, "threshold": 0.05}, "mom_sensitive"),
    # calibration
    ("calibration_bucket", {"min_edge": 0.03},                  "cal_3pct"),
    ("calibration_bucket", {"min_edge": 0.05},                  "cal_5pct"),
    # fade favorite
    ("fade_favorite",      {"min_price": 0.55, "max_price": 0.75}, "fade_55-75"),
    ("fade_favorite",      {"min_price": 0.60, "max_price": 0.80}, "fade_60-80"),
    ("fade_favorite",      {"min_price": 0.65, "max_price": 0.85}, "fade_65-85"),
    # late drift
    ("late_drift",         {"entry_window": 0.10, "drift_threshold": 0.03}, "drift_tight"),
    ("late_drift",         {"entry_window": 0.15, "drift_threshold": 0.05}, "drift_default"),
    ("late_drift",         {"entry_window": 0.25, "drift_threshold": 0.07}, "drift_wide"),
]

ENTRY_PCTS     = [0.20, 0.35, 0.50, 0.65, 0.80]
TAKE_PROFITS   = [0.55, 0.65, 0.75, 0.85, None]   # None = disabled
STOP_LOSSES    = [0.05, 0.10, 0.15, 0.20, None]    # None = disabled
SIZING_MODES   = ["fixed_dollar", "fixed_pct"]      # flat sizing only — no strategy-defined

# ── build combos ──────────────────────────────────────────────────────────────

combos = list(itertools.product(STRATEGIES, ENTRY_PCTS, TAKE_PROFITS, STOP_LOSSES, SIZING_MODES))
total  = len(combos)
print(f"Testing {total} combinations on ATP match-winner markets (flat sizing, compounding=ON) …", flush=True)

results = []
for idx, ((sname, sparams, slabel), entry_pct, tp, sl, sizing) in enumerate(combos):
    if (idx + 1) % 100 == 0:
        print(f"  {idx+1}/{total} …", flush=True)

    # skip combos where both exits are None (that's just hold-to-close — fine,
    # but redundant across sizing modes since ROI is identical; keep one)
    if tp is None and sl is None and sizing != "strategy":
        continue

    try:
        r = run_backtest(
            sport=SPORT,
            strategy_name=sname,
            strategy_params=sparams,
            entry_pct=entry_pct,
            bankroll=BANKROLL,
            hold_to_close=(tp is None and sl is None),
            take_profit_price=tp,
            stop_loss_price=sl,
            compounding=True,
            sizing_mode=sizing,
            unit_size=50.0,
            unit_pct=0.05,
            limit_orders=False,
        )
    except Exception as exc:
        continue

    if r["total_bets"] < MIN_BETS:
        continue

    results.append({
        "strategy":       slabel,
        "entry_pct":      entry_pct,
        "tp":             tp,
        "sl":             sl,
        "sizing":         sizing,
        "bets":           r["total_bets"],
        "win_rate":       r["win_rate"],
        "roi":            r["roi"],
        "total_pnl":      r["total_pnl"],
        "total_wagered":  r["total_wagered"],
        "final_bankroll": r["final_bankroll"] or (BANKROLL + r["total_pnl"]),
    })

# ── report ────────────────────────────────────────────────────────────────────

results.sort(key=lambda x: x["roi"], reverse=True)

print(f"\n{'='*105}")
print(f"{'Strategy':<20} {'Entry':>6} {'TP':>6} {'SL':>6} {'Sizing':<14} {'Bets':>5} {'WinRate':>8} {'ROI':>8} {'FinalBankroll':>14}")
print(f"{'='*110}")

for r in results[:30]:
    tp_s  = f"{r['tp']:.2f}"  if r["tp"]  is not None else " None"
    sl_s  = f"{r['sl']:.2f}"  if r["sl"]  is not None else " None"
    print(
        f"{r['strategy']:<20} {r['entry_pct']:>5.0%} {tp_s:>6} {sl_s:>6} "
        f"{r['sizing']:<14} {r['bets']:>5} {r['win_rate']:>7.1%} "
        f"{r['roi']:>+7.1%} ${r['final_bankroll']:>12,.0f}"
    )

print(f"\nTotal qualifying runs: {len(results)}")
print(f"\n--- WORST 5 ---")
for r in results[-5:]:
    tp_s = f"{r['tp']:.2f}" if r["tp"] is not None else " None"
    sl_s = f"{r['sl']:.2f}" if r["sl"] is not None else " None"
    print(
        f"{r['strategy']:<20} {r['entry_pct']:>5.0%} TP={tp_s} SL={sl_s} "
        f"{r['sizing']:<14} bets={r['bets']} ROI={r['roi']:+.1%}"
    )
