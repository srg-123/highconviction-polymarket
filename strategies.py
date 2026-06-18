"""
Strategy registry for Polymarket backtester.

Each strategy function signature:
    fn(snapshot: dict, history: list[dict], **params) -> dict

snapshot  — {"timestamp": int, "price": float}
history   — list of prior snapshots (oldest first), same shape
returns   — {"bet": "yes"|"no"|None, "size_fraction": float, "reason": str}
"""


# ── primitives ────────────────────────────────────────────────────────────────

def price_threshold(snapshot: dict, history: list, threshold: float = 0.35, side: str = "yes") -> dict:
    """Bet when price strays far from 0.5 in the favourable direction."""
    p = snapshot["price"]

    if side in ("yes", "both") and p < threshold:
        frac = min((threshold - p) / threshold * 0.15, 0.10)
        return {"bet": "yes", "size_fraction": round(frac, 4),
                "reason": f"price {p:.3f} below threshold {threshold:.2f}"}

    if side in ("no", "both") and p > (1.0 - threshold):
        frac = min((p - (1.0 - threshold)) / threshold * 0.15, 0.10)
        return {"bet": "no", "size_fraction": round(frac, 4),
                "reason": f"price {p:.3f} above fade level {1-threshold:.2f}"}

    return {"bet": None, "size_fraction": 0.0, "reason": "no signal"}


def momentum_reversal(snapshot: dict, history: list, lookback: int = 10, threshold: float = 0.08) -> dict:
    """Fade sustained price trends, expecting mean-reversion."""
    if len(history) < lookback:
        return {"bet": None, "size_fraction": 0.0, "reason": "insufficient history"}

    p = snapshot["price"]
    delta = p - history[-lookback]["price"]

    if delta < -threshold and p > 0.05:
        frac = min(abs(delta) * 0.6, 0.08)
        return {"bet": "yes", "size_fraction": round(frac, 4),
                "reason": f"reversal: price fell {delta:.3f} over {lookback} pts"}

    if delta > threshold and p < 0.95:
        frac = min(abs(delta) * 0.6, 0.08)
        return {"bet": "no", "size_fraction": round(frac, 4),
                "reason": f"reversal: price rose {delta:.3f} over {lookback} pts"}

    return {"bet": None, "size_fraction": 0.0, "reason": "no momentum signal"}


def calibration_bucket(snapshot: dict, history: list,
                        calibration: dict | None = None,
                        min_edge: float = 0.03) -> dict:
    """
    Bet when historical calibration shows a systematic bias for this price bucket.
    calibration maps bucket label -> {market_prob, actual_win_rate, count}.
    """
    if not calibration:
        return {"bet": None, "size_fraction": 0.0, "reason": "no calibration data"}

    p = snapshot["price"]
    low = int(p * 10) * 10
    label = f"{low}-{low+10}%"

    cal = calibration.get(label)
    if not cal or cal.get("count", 0) < 30:
        return {"bet": None, "size_fraction": 0.0, "reason": f"thin bucket {label}"}

    edge = cal["actual_win_rate"] - cal["market_prob"]
    if edge > min_edge:
        return {"bet": "yes", "size_fraction": round(min(edge * 0.5, 0.10), 4),
                "reason": f"cal edge +{edge:.3f} in {label}"}
    if edge < -min_edge:
        return {"bet": "no", "size_fraction": round(min(abs(edge) * 0.5, 0.10), 4),
                "reason": f"cal fade {edge:.3f} in {label}"}

    return {"bet": None, "size_fraction": 0.0, "reason": f"edge {edge:.3f} below min"}


def fade_favorite(snapshot: dict, history: list,
                  min_price: float = 0.55, max_price: float = 0.75) -> dict:
    """Fade heavy favourites: bet NO when YES is between min_price and max_price."""
    p = snapshot["price"]
    if min_price <= p <= max_price:
        frac = round(min((p - 0.50) * 0.4, 0.08), 4)
        return {"bet": "no", "size_fraction": frac,
                "reason": f"fade favourite at {p:.3f}"}
    return {"bet": None, "size_fraction": 0.0, "reason": "outside fade range"}


def late_drift(snapshot: dict, history: list,
               entry_window: float = 0.15, drift_threshold: float = 0.05) -> dict:
    """
    Bet with late-closing drift: if price moved significantly in the last
    entry_window fraction of recorded history, follow the move.
    """
    n = len(history)
    if n < 10:
        return {"bet": None, "size_fraction": 0.0, "reason": "insufficient history"}

    window = max(2, int(n * entry_window))
    delta = snapshot["price"] - history[-window]["price"]

    if abs(delta) < drift_threshold:
        return {"bet": None, "size_fraction": 0.0, "reason": "no late drift"}

    bet  = "yes" if delta > 0 else "no"
    frac = round(min(abs(delta) * 0.5, 0.08), 4)
    return {"bet": bet, "size_fraction": frac,
            "reason": f"late drift {delta:+.3f} over last {window} pts"}


# ── registry ──────────────────────────────────────────────────────────────────

STRATEGIES: dict[str, dict] = {
    "price_threshold": {
        "fn": price_threshold,
        "description": "Bet when price strays below/above a threshold",
        "params": {
            "threshold": {"type": "float", "default": 0.35, "min": 0.05, "max": 0.49,
                          "label": "Threshold"},
            "side": {"type": "select", "default": "yes",
                     "options": ["yes", "no", "both"], "label": "Side"},
        },
    },
    "momentum_reversal": {
        "fn": momentum_reversal,
        "description": "Fade sustained moves expecting mean-reversion",
        "params": {
            "lookback": {"type": "int", "default": 10, "min": 3, "max": 50,
                         "label": "Lookback periods"},
            "threshold": {"type": "float", "default": 0.08, "min": 0.02, "max": 0.30,
                          "label": "Move threshold"},
        },
    },
    "calibration_bucket": {
        "fn": calibration_bucket,
        "description": "Bet when calibration shows systematic bias in a price bucket",
        "params": {
            "min_edge": {"type": "float", "default": 0.03, "min": 0.01, "max": 0.15,
                         "label": "Min edge"},
        },
    },
    "fade_favorite": {
        "fn": fade_favorite,
        "description": "Bet NO on heavy favourites within a price band",
        "params": {
            "min_price": {"type": "float", "default": 0.55, "min": 0.50, "max": 0.85,
                          "label": "Min price"},
            "max_price": {"type": "float", "default": 0.75, "min": 0.55, "max": 0.95,
                          "label": "Max price"},
        },
    },
    "late_drift": {
        "fn": late_drift,
        "description": "Follow price drift in the final window before close",
        "params": {
            "entry_window": {"type": "float", "default": 0.15, "min": 0.05, "max": 0.40,
                             "label": "Window (fraction)"},
            "drift_threshold": {"type": "float", "default": 0.05, "min": 0.01, "max": 0.20,
                                 "label": "Drift threshold"},
        },
    },
}
