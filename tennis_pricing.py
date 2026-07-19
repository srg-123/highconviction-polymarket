# tennis_pricing.py
#
# Tennis point-by-point win-probability engine (Markov chain).
#
# Given per-point serve-win probabilities for two players, computes the live
# win probability for the match from any current score state (mid-point,
# mid-game, mid-tiebreak, mid-set). This is the pricing "brain" for an
# eventual market-making bot — it has no knowledge of live feeds, order
# books, or execution; it's a pure function of score state.
#
# Player identity is always the string "a" or "b".

import random


def _other(player: str) -> str:
    return "b" if player == "a" else "a"


def prob_win_game(p: float, pts: tuple[int, int] = (0, 0), no_ad: bool = False) -> float:
    """Probability the SERVER wins the game from point score pts=(server_pts, returner_pts)."""
    q = 1 - p
    memo: dict[tuple[int, int], float] = {}

    def solve(a: int, b: int) -> float:
        if a >= 4 and a - b >= 2:
            return 1.0
        if b >= 4 and b - a >= 2:
            return 0.0
        if no_ad and a >= 3 and b >= 3:
            return p
        if a >= 3 and b >= 3:
            d = p * p / (p * p + q * q)
            diff = a - b
            if diff == 0:
                return d
            if diff == 1:
                return p + q * d
            return p * d  # diff == -1
        key = (a, b)
        if key in memo:
            return memo[key]
        result = p * solve(a + 1, b) + q * solve(a, b + 1)
        memo[key] = result
        return result

    return solve(*pts)


def tiebreak_server(a: int, b: int, first_server: str) -> str:
    """Who serves the next point in a tiebreak, given points played so far (a, b)."""
    n = a + b + 1
    if n == 1:
        return first_server
    block = (n - 2) // 2
    return first_server if block % 2 == 1 else _other(first_server)


def prob_win_tiebreak(
    p_a: float,
    p_b: float,
    pts: tuple[int, int] = (0, 0),
    first_server: str = "a",
    target: int = 7,
    win_by: int = 2,
) -> float:
    """Probability A wins the tiebreak from point score pts=(a_pts, b_pts).

    Past a tie at (n, n), server alternation is periodic with period 2 in n,
    which means the "ahead by 1 -> win outright or return to a higher tie"
    dynamics form a fixed 2-state linear system once n is high enough that
    winning the next 2 points always reaches `target` (n >= target - win_by).
    Below that, plain recursion (bounded by target) is used. Without this,
    naive recursion over an unbounded tied-score random walk never
    terminates (stack overflow) — this isn't an optimization, it's required
    for correctness/termination, unlike the game-level memoization.
    """

    def point_prob_a(server: str) -> float:
        return p_a if server == "a" else (1 - p_b)

    n0 = max(target - win_by, 0)
    extended: dict[str, float] = {}
    if win_by == 2:

        def coeffs(n: int) -> tuple[float, float]:
            s1 = tiebreak_server(n, n, first_server)
            p1a = point_prob_a(s1)
            s2a = tiebreak_server(n + 1, n, first_server)
            p2a = point_prob_a(s2a)
            s2b = tiebreak_server(n, n + 1, first_server)
            p2b = point_prob_a(s2b)
            c = p1a * p2a
            d = p1a * (1 - p2a) + (1 - p1a) * p2b
            return c, d

        c_even, d_even = coeffs(n0 if n0 % 2 == 0 else n0 + 1)
        c_odd, d_odd = coeffs(n0 if n0 % 2 == 1 else n0 + 1)
        denom = 1 - d_even * d_odd
        x_even = (c_even + d_even * c_odd) / denom  # f(n) for even n >= n0
        y_odd = c_odd + d_odd * x_even  # f(n) for odd n >= n0
        extended = {"even": x_even, "odd": y_odd}

    memo: dict[tuple[int, int], float] = {}

    def solve(a: int, b: int) -> float:
        if a >= target and a - b >= win_by:
            return 1.0
        if b >= target and b - a >= win_by:
            return 0.0
        if win_by == 2 and a == b and a >= n0:
            return extended["even"] if a % 2 == 0 else extended["odd"]
        key = (a, b)
        if key in memo:
            return memo[key]
        server = tiebreak_server(a, b, first_server)
        if server == "a":
            result = p_a * solve(a + 1, b) + (1 - p_a) * solve(a, b + 1)
        else:
            result = (1 - p_b) * solve(a + 1, b) + p_b * solve(a, b + 1)
        memo[key] = result
        return result

    return solve(*pts)


def prob_win_set(
    p_a_serve: float,
    p_b_serve: float,
    games: tuple[int, int] = (0, 0),
    server: str = "a",
    points: tuple[int, int] = (0, 0),
    in_tiebreak: bool = False,
    no_ad: bool = False,
) -> float:
    """Probability A wins the set from an arbitrary mid-set state."""
    memo: dict[tuple[int, int, str, tuple[int, int]], float] = {}

    def solve(a: int, b: int, srv: str, pts: tuple[int, int], tb: bool) -> float:
        if a >= 6 and a - b >= 2:
            return 1.0
        if b >= 6 and b - a >= 2:
            return 0.0
        if tb or (a == 6 and b == 6):
            return prob_win_tiebreak(p_a_serve, p_b_serve, pts=pts, first_server=srv)
        key = (a, b, srv, pts)
        if key in memo:
            return memo[key]
        if srv == "a":
            p_game_a = prob_win_game(p_a_serve, pts=pts, no_ad=no_ad)
        else:
            p_game_a = 1 - prob_win_game(p_b_serve, pts=(pts[1], pts[0]), no_ad=no_ad)
        nxt = _other(srv)
        result = p_game_a * solve(a + 1, b, nxt, (0, 0), False) + (1 - p_game_a) * solve(
            a, b + 1, nxt, (0, 0), False
        )
        memo[key] = result
        return result

    return solve(games[0], games[1], server, points, in_tiebreak)


def prob_win_match(
    p_a_serve: float,
    p_b_serve: float,
    sets: tuple[int, int] = (0, 0),
    games: tuple[int, int] = (0, 0),
    server: str = "a",
    points: tuple[int, int] = (0, 0),
    in_tiebreak: bool = False,
    best_of: int = 3,
    no_ad: bool = False,
) -> float:
    """Probability A wins the match from an arbitrary current state.

    Server alternation is tracked continuously across set boundaries (a
    tiebreak counts as exactly one game) rather than reset per set — that's
    the actual ITF rule, not a simplification.
    """
    needed = -(-best_of // 2)  # ceil(best_of / 2)
    memo: dict[tuple[int, int, int, int, str, tuple[int, int]], float] = {}

    def solve(sa: int, sb: int, ga: int, gb: int, srv: str, pts: tuple[int, int], tb: bool) -> float:
        if sa >= needed:
            return 1.0
        if sb >= needed:
            return 0.0
        if ga >= 6 and ga - gb >= 2:
            return solve(sa + 1, sb, 0, 0, srv, (0, 0), False)
        if gb >= 6 and gb - ga >= 2:
            return solve(sa, sb + 1, 0, 0, srv, (0, 0), False)
        if tb or (ga == 6 and gb == 6):
            p_set_a = prob_win_tiebreak(p_a_serve, p_b_serve, pts=pts, first_server=srv)
            nxt = _other(srv)
            return p_set_a * solve(sa + 1, sb, 0, 0, nxt, (0, 0), False) + (1 - p_set_a) * solve(
                sa, sb + 1, 0, 0, nxt, (0, 0), False
            )
        key = (sa, sb, ga, gb, srv, pts)
        if key in memo:
            return memo[key]
        if srv == "a":
            p_game_a = prob_win_game(p_a_serve, pts=pts, no_ad=no_ad)
        else:
            p_game_a = 1 - prob_win_game(p_b_serve, pts=(pts[1], pts[0]), no_ad=no_ad)
        nxt = _other(srv)
        result = p_game_a * solve(sa, sb, ga + 1, gb, nxt, (0, 0), False) + (1 - p_game_a) * solve(
            sa, sb, ga, gb + 1, nxt, (0, 0), False
        )
        memo[key] = result
        return result

    return solve(sets[0], sets[1], games[0], games[1], server, points, in_tiebreak)


def live_win_probability(
    state: dict, p_a_serve: float, p_b_serve: float, best_of: int = 3
) -> dict:
    """Full match state -> {'a': prob_a_wins_match, 'b': prob_b_wins_match}.

    `state` is a plain dict (designed for incremental point-by-point mutation
    by a future live feed): sets, games, points, server, in_tiebreak, no_ad,
    best_of.
    """
    best_of = state.get("best_of", best_of)
    a_wins = prob_win_match(
        p_a_serve,
        p_b_serve,
        sets=state["sets"],
        games=state["games"],
        server=state["server"],
        points=state["points"],
        in_tiebreak=state["in_tiebreak"],
        best_of=best_of,
        no_ad=state.get("no_ad", False),
    )
    return {"a": a_wins, "b": 1.0 - a_wins}


def simulate_match(
    p_a_serve: float,
    p_b_serve: float,
    best_of: int = 3,
    no_ad: bool = False,
    first_server: str = "a",
    rng: random.Random | None = None,
) -> str:
    """Point-by-point Monte Carlo simulator. Returns 'a' or 'b' (match winner).

    Deliberately independent of the recursive formulas above (no shared
    memoization or helpers beyond tiebreak_server/_other) — used purely to
    cross-validate them.
    """
    rng = rng or random

    def play_game(srv: str) -> str:
        pts_a = pts_b = 0
        point_prob_a = p_a_serve if srv == "a" else (1 - p_b_serve)
        while True:
            if rng.random() < point_prob_a:
                pts_a += 1
            else:
                pts_b += 1
            if no_ad:
                if pts_a >= 4:
                    return "a"
                if pts_b >= 4:
                    return "b"
            else:
                if pts_a >= 4 and pts_a - pts_b >= 2:
                    return "a"
                if pts_b >= 4 and pts_b - pts_a >= 2:
                    return "b"

    def play_tiebreak(first_srv: str) -> str:
        pts_a = pts_b = 0
        while True:
            srv = tiebreak_server(pts_a, pts_b, first_srv)
            point_prob_a = p_a_serve if srv == "a" else (1 - p_b_serve)
            if rng.random() < point_prob_a:
                pts_a += 1
            else:
                pts_b += 1
            if pts_a >= 7 and pts_a - pts_b >= 2:
                return "a"
            if pts_b >= 7 and pts_b - pts_a >= 2:
                return "b"

    def play_set(first_srv: str):
        games_a = games_b = 0
        srv = first_srv
        while True:
            if games_a >= 6 and games_a - games_b >= 2:
                return "a", srv
            if games_b >= 6 and games_b - games_a >= 2:
                return "b", srv
            if games_a == 6 and games_b == 6:
                winner = play_tiebreak(srv)
                return winner, _other(srv)
            winner = play_game(srv)
            if winner == "a":
                games_a += 1
            else:
                games_b += 1
            srv = _other(srv)

    needed = -(-best_of // 2)
    sets_a = sets_b = 0
    server = first_server
    while True:
        winner, server = play_set(server)
        if winner == "a":
            sets_a += 1
        else:
            sets_b += 1
        if sets_a >= needed:
            return "a"
        if sets_b >= needed:
            return "b"


def iter_points(
    p_a_serve: float,
    p_b_serve: float,
    best_of: int = 3,
    no_ad: bool = False,
    first_server: str = "a",
    rng: random.Random | None = None,
):
    """Yields the full match state (same shape live_win_probability consumes)
    after every single point, until the match ends.

    A fresh, flat state machine — not a retrofit of simulate_match's nested
    closures, which are validated specifically as return-final-winner-only
    functions. Reuses the same rule helpers (tiebreak_server, _other) so the
    two independent implementations can be cross-checked against each other.
    """
    rng = rng or random
    needed = -(-best_of // 2)
    sets_a = sets_b = games_a = games_b = pts_a = pts_b = 0
    server = first_server
    in_tiebreak = False

    def state() -> dict:
        return {
            "best_of": best_of,
            "sets": (sets_a, sets_b),
            "games": (games_a, games_b),
            "points": (pts_a, pts_b),
            "server": server,
            "in_tiebreak": in_tiebreak,
            "no_ad": no_ad,
        }

    while True:
        cur_server = tiebreak_server(pts_a, pts_b, server) if in_tiebreak else server
        point_prob_a = p_a_serve if cur_server == "a" else (1 - p_b_serve)
        if rng.random() < point_prob_a:
            pts_a += 1
        else:
            pts_b += 1

        if not in_tiebreak:
            a_won = pts_a >= 4 if no_ad else (pts_a >= 4 and pts_a - pts_b >= 2)
            b_won = pts_b >= 4 if no_ad else (pts_b >= 4 and pts_b - pts_a >= 2)
            if a_won or b_won:
                if a_won:
                    games_a += 1
                else:
                    games_b += 1
                pts_a = pts_b = 0
                server = _other(server)
                if games_a >= 6 and games_a - games_b >= 2:
                    sets_a += 1
                    games_a = games_b = 0
                elif games_b >= 6 and games_b - games_a >= 2:
                    sets_b += 1
                    games_a = games_b = 0
                elif games_a == 6 and games_b == 6:
                    in_tiebreak = True
        else:
            if (pts_a >= 7 and pts_a - pts_b >= 2) or (pts_b >= 7 and pts_b - pts_a >= 2):
                if pts_a > pts_b:
                    sets_a += 1
                else:
                    sets_b += 1
                games_a = games_b = pts_a = pts_b = 0
                in_tiebreak = False
                server = _other(server)

        yield state()
        if sets_a >= needed or sets_b >= needed:
            return


def advance_state(
    state: dict,
    point_winner: str,
    p_a_serve: float,
    p_b_serve: float,
    best_of: int = 3,
) -> dict:
    """Pure state transition: given a hypothetical winner of the NEXT point,
    return the resulting state.

    Mirrors iter_points' rollover logic (point -> game -> tiebreak -> set)
    rather than naively bumping `points` by one. That shortcut has a real
    trap: prob_win_game's no_ad deuce guard (`a >= 3 and b >= 3 -> return p`)
    would wrongly re-fire on a bumped (4, 3) state instead of resolving to a
    terminal 1.0/0.0, since it only ever sees (3, 3) via the module's own
    internal recursion. Implementing the transition at the state level (as
    here) never hands prob_win_game/prob_win_tiebreak a state their base
    cases weren't built for.
    """
    sets_a, sets_b = state["sets"]
    games_a, games_b = state["games"]
    pts_a, pts_b = state["points"]
    server = state["server"]
    in_tiebreak = state["in_tiebreak"]
    no_ad = state.get("no_ad", False)
    best_of = state.get("best_of", best_of)

    if point_winner == "a":
        pts_a += 1
    else:
        pts_b += 1

    if not in_tiebreak:
        a_won = pts_a >= 4 if no_ad else (pts_a >= 4 and pts_a - pts_b >= 2)
        b_won = pts_b >= 4 if no_ad else (pts_b >= 4 and pts_b - pts_a >= 2)
        if a_won or b_won:
            if a_won:
                games_a += 1
            else:
                games_b += 1
            pts_a = pts_b = 0
            server = _other(server)
            if games_a >= 6 and games_a - games_b >= 2:
                sets_a += 1
                games_a = games_b = 0
            elif games_b >= 6 and games_b - games_a >= 2:
                sets_b += 1
                games_a = games_b = 0
            elif games_a == 6 and games_b == 6:
                in_tiebreak = True
    else:
        if (pts_a >= 7 and pts_a - pts_b >= 2) or (pts_b >= 7 and pts_b - pts_a >= 2):
            if pts_a > pts_b:
                sets_a += 1
            else:
                sets_b += 1
            games_a = games_b = pts_a = pts_b = 0
            in_tiebreak = False
            server = _other(server)

    return {
        "best_of": best_of,
        "sets": (sets_a, sets_b),
        "games": (games_a, games_b),
        "points": (pts_a, pts_b),
        "server": server,
        "in_tiebreak": in_tiebreak,
        "no_ad": no_ad,
    }


def point_sensitivity(state: dict, p_a_serve: float, p_b_serve: float, best_of: int = 3) -> float:
    """How much is riding on the very next point from here.

    abs(fair value if A wins the next point - fair value if B wins it) — a
    local, model-implied instantaneous-variance proxy (not a historical
    estimate), naturally high near game/set/match points and near-zero at
    routine points. Used as the domain-native stand-in for sigma/(T-t) in
    the inventory-skew layer, since match length is a random stopping time
    and the process gets MORE volatile approaching resolution, not less.
    """
    best_of = state.get("best_of", best_of)
    hyp_a = advance_state(state, "a", p_a_serve, p_b_serve, best_of)
    hyp_b = advance_state(state, "b", p_a_serve, p_b_serve, best_of)
    fa = live_win_probability(hyp_a, p_a_serve, p_b_serve, best_of)["a"]
    fb = live_win_probability(hyp_b, p_a_serve, p_b_serve, best_of)["a"]
    return abs(fa - fb)


if __name__ == "__main__":
    assert abs(prob_win_game(0.5) - 0.5) < 1e-9
    assert abs(prob_win_tiebreak(0.5, 0.5) - 0.5) < 1e-9
    assert abs(prob_win_set(0.5, 0.5) - 0.5) < 1e-9
    assert abs(prob_win_match(0.5, 0.5) - 0.5) < 1e-9
    print("symmetry checks passed")

    ps = [0.50, 0.55, 0.60, 0.65, 0.70]
    vals = [prob_win_match(p, 0.60, best_of=3) for p in ps]
    assert all(vals[i] < vals[i + 1] for i in range(len(vals) - 1))
    print("monotonicity check passed:", [round(v, 4) for v in vals])

    rng = random.Random(42)
    N = 20_000
    for p_a, p_b, bo in [(0.65, 0.60, 3), (0.62, 0.58, 5), (0.55, 0.50, 3)]:
        formula = prob_win_match(p_a, p_b, best_of=bo)
        wins_a = sum(1 for _ in range(N) if simulate_match(p_a, p_b, best_of=bo, rng=rng) == "a")
        mc = wins_a / N
        diff = abs(formula - mc)
        status = "OK" if diff < 0.01 else "FAIL"
        print(f"[{status}] p_a={p_a} p_b={p_b} bo={bo}  formula={formula:.4f}  mc={mc:.4f}  diff={diff:.4f}")

    leading_state = {
        "best_of": 3,
        "sets": (1, 0),
        "games": (5, 2),
        "points": (0, 0),
        "server": "a",
        "in_tiebreak": False,
        "no_ad": False,
    }
    result = live_win_probability(leading_state, 0.65, 0.60)
    print("leading-state win prob:", result)
    assert result["a"] > 0.85

    no_ad_val = prob_win_game(0.65, pts=(3, 3), no_ad=True)
    assert abs(no_ad_val - 0.65) < 1e-9
    print("no_ad deuce shortcut check passed:", no_ad_val)

    p_a, p_b, bo = 0.65, 0.60, 3
    formula = prob_win_match(p_a, p_b, best_of=bo)
    rng = random.Random(7)
    wins_a = 0
    for _ in range(N):
        for s in iter_points(p_a, p_b, best_of=bo, rng=rng):
            pass
        wins_a += 1 if s["sets"][0] > s["sets"][1] else 0
    mc_iter = wins_a / N
    diff = abs(formula - mc_iter)
    status = "OK" if diff < 0.01 else "FAIL"
    print(f"[{status}] iter_points cross-check  formula={formula:.4f}  mc={mc_iter:.4f}  diff={diff:.4f}")

    prev = {
        "best_of": 3,
        "sets": (0, 0),
        "games": (0, 0),
        "points": (0, 0),
        "server": "a",
        "in_tiebreak": False,
        "no_ad": False,
    }
    rng = random.Random(123)
    for nxt in iter_points(0.62, 0.58, best_of=3, rng=rng):
        hyp_a = advance_state(prev, "a", 0.62, 0.58, best_of=3)
        hyp_b = advance_state(prev, "b", 0.62, 0.58, best_of=3)
        assert nxt in (hyp_a, hyp_b), f"advance_state disagrees with iter_points: {prev} -> {nxt}"
        prev = nxt
    print("advance_state cross-check passed")

    terminal_state = {
        "best_of": 3,
        "sets": (2, 0),
        "games": (0, 0),
        "points": (0, 0),
        "server": "a",
        "in_tiebreak": False,
        "no_ad": False,
    }
    sens = point_sensitivity(terminal_state, 0.65, 0.60)
    assert sens == 0.0
    print("point_sensitivity terminal-state check passed:", sens)
