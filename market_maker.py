# market_maker.py
#
# Paper-trading quote engine for the tennis market-making bot.
#
# Posts symmetric bid/ask quotes around the tennis_pricing fair value and
# simulates fills point-by-point. No live feed and no real order book yet —
# fills are simulated against our OWN re-priced fair value, since there's no
# external counterparty data available (a v1 simplification, not a stand-in
# for real market fills). Inventory-skew, live data, and SQLite markout/CLV
# persistence are separate future phases, not built here.

import random

from tennis_pricing import iter_points, live_win_probability, point_sensitivity


def quote(fair: float, half_spread: float = 0.02, min_price: float = 0.01, max_price: float = 0.99) -> dict:
    bid = max(min_price, min(fair - half_spread, max_price))
    ask = max(min_price, min(fair + half_spread, max_price))
    if bid >= ask:
        tick = 0.01
        bid = max(min_price, min(fair, max_price) - tick)
        ask = min(max_price, bid + tick)
    return {"bid": round(bid, 4), "ask": round(ask, 4)}


def skewed_quote(
    fair: float,
    position: float,
    sensitivity: float,
    half_spread: float = 0.02,
    gamma: float = 0.02,
    gamma_spread: float | None = None,
    min_price: float = 0.01,
    max_price: float = 0.99,
) -> dict:
    """Avellaneda-Stoikov-inspired inventory skew, adapted to a discrete,
    resolving probability process rather than a literal port of the
    continuous-time formula.

    `sensitivity` (from tennis_pricing.point_sensitivity) stands in for
    sigma/(T-t): it's a local, model-implied "how much is riding on the next
    point" measure that's naturally high near game/set/match points, unlike
    a constant sigma or a shrinking fixed horizon (match length is a random
    stopping time, and this process gets MORE volatile approaching
    resolution, not less).

    Long position (position > 0) pulls the reservation price below fair —
    discourages buying more A, encourages getting lifted out of it; short
    position does the opposite. gamma/gamma_spread are untuned v1
    placeholders (no calibration data exists, same constraint as the rest
    of this project) — gamma_spread defaults to gamma so there's one
    effective knob unless deliberately decoupled.
    """
    gamma_spread = gamma if gamma_spread is None else gamma_spread
    risk = sensitivity * sensitivity  # variance-like, mirrors AS's sigma^2
    reservation = fair - gamma * position * risk
    half_spread_eff = half_spread + 0.5 * gamma_spread * risk
    q = quote(reservation, half_spread=half_spread_eff, min_price=min_price, max_price=max_price)
    raw_bid = max(min_price, min(reservation - half_spread_eff, max_price))
    raw_ask = max(min_price, min(reservation + half_spread_eff, max_price))
    q["fallback"] = raw_bid >= raw_ask
    return q


def check_fill(resting: dict, new_fair: float) -> str | None:
    """Which side of `resting` the new fair value has traded through, if any.

    This is the "toxic" fill mechanism: it only fires when the true fair
    value has already moved past our quote, so it's informationally adverse
    to us by construction — see check_noise_fill for the informationless
    counterpart.
    """
    if new_fair >= resting["ask"]:
        return "ask"
    if new_fair <= resting["bid"]:
        return "bid"
    return None


def check_noise_fill(rng: random.Random, prob: float = 0.05) -> str | None:
    """Independent liquidity-taker arrival, uncorrelated with fair value.

    With probability `prob` a noise trader arrives this point and hits one
    of our two resting quotes at random (50/50 bid/ask) — modeling
    informationless order flow, as opposed to check_fill's informed/toxic
    crossing. Takes no `resting`/`new_fair` input on purpose: the decision
    must carry zero information about where fair value is or is heading.
    """
    if rng.random() >= prob:
        return None
    return "bid" if rng.random() < 0.5 else "ask"


def apply_fill(position: float, cash: float, side: str, price: float, fill_size_usd: float) -> tuple[float, float]:
    contracts = fill_size_usd / price
    if side == "bid":  # we bought A
        return position + contracts, cash - fill_size_usd
    return position - contracts, cash + fill_size_usd  # side == "ask": we sold A


def settle(position: float, cash: float, winner: str) -> float:
    settle_price = 1.0 if winner == "a" else 0.0
    return cash + position * settle_price


def markout(fair_history: list[float], fill: dict, horizon: int = 5) -> float:
    """Signed fair-value move `horizon` points after the fill.

    Positive: the move favored the side we took (bid wants fair to rise
    later, ask wants it to fall). Negative average across fills means we're
    getting adversely selected.
    """
    i = fill["point_idx"]
    j = min(i + horizon, len(fair_history) - 1)
    move = fair_history[j] - fill["fair_at_fill"]
    sign = 1.0 if fill["side"] == "bid" else -1.0
    return sign * move


def run_paper_trading_session(
    p_a_serve: float,
    p_b_serve: float,
    best_of: int = 3,
    no_ad: bool = False,
    half_spread: float = 0.02,
    fill_size_usd: float = 10.0,
    noise_fill_prob: float = 0.05,
    use_skew: bool = False,
    gamma: float = 0.02,
    gamma_spread: float | None = None,
    rng: random.Random | None = None,
    noise_rng: random.Random | None = None,
) -> dict:
    rng = rng or random
    # Dedicated child stream, seeded off one extra draw from `rng` — never
    # noise_rng = rng. `rng` also drives iter_points' point outcomes, so
    # sharing a stream would mean changing noise_fill_prob shifts every
    # subsequent point draw, changing the whole match trajectory rather than
    # isolating the effect to fills/PnL alone.
    noise_rng = noise_rng or random.Random(rng.random())
    position = 0.0
    cash = 0.0
    fills: list[dict] = []
    fair_history: list[float] = []
    resting: dict | None = None
    winner = None
    fallback_count = 0
    max_abs_position = 0.0

    for point_idx, state in enumerate(iter_points(p_a_serve, p_b_serve, best_of=best_of, no_ad=no_ad, rng=rng)):
        new_fair = live_win_probability(state, p_a_serve, p_b_serve)["a"]

        # Check the OLD resting quote against the NEW fair value before
        # requoting — a freshly re-centered quote can never be crossed by
        # the value it was just centered on.
        filled_sides: set[str] = set()
        if resting is not None:
            side = check_fill(resting, new_fair)
            if side is not None:
                price = resting[side]
                position, cash = apply_fill(position, cash, side, price, fill_size_usd)
                max_abs_position = max(max_abs_position, abs(position))
                fills.append(
                    {"point_idx": point_idx, "side": side, "price": price, "fair_at_fill": new_fair, "reason": "toxic"}
                )
                filled_sides.add(side)

            noise_side = check_noise_fill(noise_rng, prob=noise_fill_prob)
            if noise_side is not None and noise_side not in filled_sides:
                price = resting[noise_side]
                position, cash = apply_fill(position, cash, noise_side, price, fill_size_usd)
                max_abs_position = max(max_abs_position, abs(position))
                fills.append(
                    {
                        "point_idx": point_idx,
                        "side": noise_side,
                        "price": price,
                        "fair_at_fill": new_fair,
                        "reason": "noise",
                    }
                )

        fair_history.append(new_fair)
        if use_skew:
            sensitivity = point_sensitivity(state, p_a_serve, p_b_serve, best_of=best_of)
            resting = skewed_quote(
                new_fair, position, sensitivity, half_spread=half_spread, gamma=gamma, gamma_spread=gamma_spread
            )
            if resting["fallback"]:
                fallback_count += 1
        else:
            resting = quote(new_fair, half_spread=half_spread)

        sets_a, sets_b = state["sets"]
        needed = -(-best_of // 2)
        if sets_a >= needed:
            winner = "a"
        elif sets_b >= needed:
            winner = "b"

    pnl = settle(position, cash, winner)
    return {
        "pnl": pnl,
        "fills": fills,
        "fair_history": fair_history,
        "winner": winner,
        "fallback_count": fallback_count,
        "max_abs_position": max_abs_position,
    }


def _summarize(results: list[dict]) -> dict:
    total_pnl = sum(r["pnl"] for r in results)
    toxic_fills = sum(1 for r in results for f in r["fills"] if f["reason"] == "toxic")
    noise_fills = sum(1 for r in results for f in r["fills"] if f["reason"] == "noise")
    markouts_by_reason: dict[str, list[float]] = {"toxic": [], "noise": []}
    for r in results:
        for f in r["fills"]:
            markouts_by_reason[f["reason"]].append(markout(r["fair_history"], f))
    avg_markout = {
        reason: (sum(vals) / len(vals) if vals else 0.0) for reason, vals in markouts_by_reason.items()
    }
    max_abs_position = max((r["max_abs_position"] for r in results), default=0.0)
    fallback_count = sum(r["fallback_count"] for r in results)
    return {
        "total_pnl": total_pnl,
        "toxic_fills": toxic_fills,
        "noise_fills": noise_fills,
        "avg_markout": avg_markout,
        "max_abs_position": max_abs_position,
        "fallback_count": fallback_count,
    }


if __name__ == "__main__":
    seed_rng = random.Random(11)
    N = 500
    p_a_serve, p_b_serve = 0.65, 0.60
    seeds = [seed_rng.random() for _ in range(N)]

    noskew_results = []
    skew_results = []
    paired_deltas = []
    for s in seeds:
        r_noskew = run_paper_trading_session(p_a_serve, p_b_serve, rng=random.Random(s), use_skew=False)
        r_skew = run_paper_trading_session(p_a_serve, p_b_serve, rng=random.Random(s), use_skew=True)
        noskew_results.append(r_noskew)
        skew_results.append(r_skew)
        paired_deltas.append(r_skew["pnl"] - r_noskew["pnl"])

    noskew_summary = _summarize(noskew_results)
    skew_summary = _summarize(skew_results)
    skew_win_pct = 100 * sum(1 for d in paired_deltas if d > 0) / N

    print(f"matches simulated:     {N}")
    print()
    for label, summary in [("no-skew", noskew_summary), ("skew", skew_summary)]:
        print(f"--- {label} ---")
        print(f"total pnl ($):         {summary['total_pnl']:.2f}")
        print(f"toxic fills:           {summary['toxic_fills']}")
        print(f"noise fills:           {summary['noise_fills']}")
        print(f"avg toxic markout@5:   {summary['avg_markout']['toxic']:+.4f}")
        print(f"avg noise markout@5:   {summary['avg_markout']['noise']:+.4f}")
        print(f"max abs position:      {summary['max_abs_position']:.2f}")
        print(f"quote fallback count:  {summary['fallback_count']}")
        print()

    print(f"paired pnl delta (skew - no-skew): {sum(paired_deltas):.2f} total, {skew_win_pct:.1f}% of matches favor skew")
