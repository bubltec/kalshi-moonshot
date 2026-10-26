"""Refit the forecast weights (forecast.py) from the tape, and check the lane's own forecasts against what followed.

fit      every moment of the recorded 15-minute crypto windows, from minute 5 to 2 minutes before the close, for the side the market leans toward (mid 55c or more, ask up to 92c, spread
         8c or less): the six index slices (forecast.EDGES) and the move of that side's mid over the next `horizon` minutes. Least squares gives the weights; the moments themselves are kept
         as the case memory. The file is written whole and swapped in, so a reader never sees half of it, and one line per fit goes to forecast_fits.jsonl next to it.
review   for the decisions a forecast lane made (the estimator saved its forecast in the decision's context): the move the side's mid actually made over the horizon, against what was forecast.
Both read the tape and the main database; neither places an order.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import os
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from . import db, forecast
from .config import Settings
from .kalshiws import index_for_series
from .store import Store
from .strategy import fee_per_contract

log = logging.getLogger("moonshot")

CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
STEP = 30.0  # seconds between sampled moments
FIRST_MINUTE, LAST_LEFT = 5.0, 120.0  # sample from minute 5 to two minutes before the close


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(a)
    m = [[*row[:], b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c], strict=True)]
    return [m[i][n] / m[i][i] for i in range(n)]


def least_squares(xs: list[list[float]], ys: list[float]) -> list[float]:
    k = len(xs[0]) + 1
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for x, y in zip(xs, ys, strict=True):
        row = [1.0, *x]
        for i in range(k):
            xty[i] += row[i] * y
            for j in range(k):
                xtx[i][j] += row[i] * row[j]
    return _solve(xtx, xty)


def _scales(ticks: dict[str, tuple[list[float], list[float]]]) -> dict[str, dict[str, float]]:
    """Each coin's usual log move over each look-back (the root mean square of its moves sampled every two minutes)."""
    out: dict[str, dict[str, float]] = {}
    for ix, (ts, vals) in ticks.items():
        out[ix] = {}
        for w in forecast.EDGES[1:]:
            rets, t = [], ts[0] + w * 60.0 + 60.0
            while t < ts[-1]:
                a, b = bisect.bisect_right(ts, t) - 1, bisect.bisect_right(ts, t - w * 60.0) - 1
                rets.append(math.log(vals[a] / vals[b]))
                t += 120.0
            out[ix][str(w)] = math.sqrt(sum(x * x for x in rets) / len(rets)) if rets else 0.0
    return out


def collect(s: Settings, horizon: int, before: float | None = None) -> tuple[list[list[float]], list[float], float, dict[str, dict[str, float]]]:
    """(slice moves per moment, the move that followed, the latest window close used, the coin scales)."""
    tape = db.connect(s.tape_path, "tape", shared=True)
    store = Store(s.db_path, s.exclude_versions)
    ticks: dict[str, tuple[list[float], list[float]]] = {}
    for ix in {index_for_series(x) for x in CRYPTO} - {None}:
        r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND value > 0 ORDER BY ts", (ix,)).fetchall()
        if len(r) > 1000:
            ticks[str(ix)] = ([x[0] for x in r], [x[1] for x in r])
    scales = _scales(ticks)
    weights = forecast.Weights({"beta": [0.0] * len(forecast.EDGES), "sigma": scales, "horizon": horizon})
    xs: list[list[float]] = []
    ys: list[float] = []
    last = 0.0
    for m in store.db.execute(f"SELECT ticker, series, close_ts FROM windows WHERE series IN ({','.join('?' * len(CRYPTO))}) AND result IN ('yes','no')" + ("" if before is None else f" AND close_ts < {float(before)}"), CRYPTO).fetchall():
        ix = index_for_series(m["series"])
        if ix is None or ix not in ticks:
            continue
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"],)).fetchall()
        if len(q) < 60:
            continue
        qts = [r[0] for r in q]
        close = float(m["close_ts"])
        t = close - 900.0 + FIRST_MINUTE * 60.0
        while t < close - LAST_LEFT:
            i = bisect.bisect_right(qts, t) - 1
            t += STEP
            if i < 0:
                continue
            by, ay = q[i][1], q[i][2]
            side = "yes" if (by + ay) / 2 >= 0.5 else "no"
            a, b = (ay, by) if side == "yes" else (1 - by, 1 - ay)
            if not ((a + b) / 2 >= 0.55 and a <= 0.92 and a - b <= 0.08):
                continue
            j = bisect.bisect_right(qts, qts[i] + horizon * 60.0) - 1
            if j <= i or qts[j] > close - 5.0:
                continue
            moves = _slice_moves_from(ticks[str(ix)], weights, str(ix), side, qts[i])
            if moves is None:
                continue
            fm = (q[j][1] + q[j][2]) / 2 if side == "yes" else 1 - (q[j][1] + q[j][2]) / 2
            xs.append(moves)
            ys.append(fm - (a + b) / 2)
            last = max(last, close)
    return xs, ys, last, scales


def _slice_moves_from(tk: tuple[list[float], list[float]], weights: forecast.Weights, ix: str, side: str, now: float) -> list[float] | None:
    """The same six slice moves as forecast.slice_moves, read from tick arrays already in memory instead of the tape."""
    ts, vals = tk
    pts = []
    for m in forecast.EDGES:
        i = bisect.bisect_right(ts, now - m * 60.0) - 1
        if i < 0 or (now - m * 60.0) - ts[i] >= forecast.MAX_AGE:
            return None
        pts.append(vals[i])
    sign = 1.0 if side == "yes" else -1.0
    out = []
    for n in range(len(forecast.EDGES) - 1):
        lo, hi = forecast.EDGES[n], forecast.EDGES[n + 1]
        usual = weights.sigma[ix].get(hi)
        if not usual:
            return None
        out.append(sign * math.log(pts[n] / pts[n + 1]) / (usual * math.sqrt((hi - lo) / hi)))
    return out


def build(s: Settings, horizon: int, before: float | None = None, k: int = 100, blend: float = 0.5, keep_cases: int = 8000) -> dict[str, Any]:
    """The weights and case memory fitted on windows that closed before `before` (all of them when None)."""
    xs, ys, cutoff, scales = collect(s, horizon, before)
    if len(xs) < 500:
        raise RuntimeError(f"only {len(xs)} moments on the tape; not fitting")
    beta = least_squares(xs, ys)
    step = max(1, len(xs) // keep_cases)
    return {"horizon": horizon, "windows": list(forecast.EDGES[1:]), "beta": beta, "sigma": scales, "cutoff": cutoff, "fitted_at": time.time(), "moments": len(xs), "k": k, "blend": blend,
            "cases": [[round(v, 4) for v in x] + [round(y, 5)] for x, y in zip(xs[::step], ys[::step], strict=True)]}


def fit(s: Settings, horizon: int, out_path: str, k: int = 100, blend: float = 0.5, keep_cases: int = 8000) -> dict[str, Any]:
    raw = build(s, horizon, None, k, blend, keep_cases)
    beta, moments = raw["beta"], raw["moments"]
    tmp = f"{out_path}.tmp"
    with open(tmp, "w") as f:
        json.dump(raw, f)
    os.replace(tmp, out_path)  # swapped in whole
    with open(os.path.join(os.path.dirname(out_path) or ".", "forecast_fits.jsonl"), "a") as f:
        f.write(json.dumps({"fitted_at": raw["fitted_at"], "horizon": horizon, "moments": moments, "cutoff": raw["cutoff"], "beta": [round(b, 5) for b in beta]}) + "\n")
    log.info("forecast refit: %d moments, horizon %d min, weights %s", moments, horizon, [round(b, 4) for b in beta])
    return raw


def _mid(row: Any, side: str) -> float:
    m = (row[1] + row[2]) / 2
    return m if side == "yes" else 1.0 - m


def review(s: Settings, lane: str, hours: float = 24.0) -> list[str]:
    """What the lane's forecasts were worth: for each decision that saved one, the move of the side's mid over that forecast's horizon."""
    tape = db.connect(s.tape_path, "tape", shared=True)
    store = Store(s.db_path, s.exclude_versions)
    rows = store.db.execute("SELECT ts, ticker, side, action, close_time, context FROM decisions WHERE lane=? AND ts>? AND context LIKE '{%' ORDER BY ts", (lane, time.time() - hours * 3600.0)).fetchall()
    got: list[tuple[float, float, float | None, str]] = []
    for r in rows:
        try:
            c = json.loads(r["context"])
            move, horizon = float(c["move"]), float(c["horizon"])
        except (ValueError, KeyError, TypeError):
            continue
        ends = r["ts"] + horizon * 60.0
        if ends > time.time() - 30.0:
            continue  # the horizon is not over yet
        if r["close_time"] and ends > datetime.fromisoformat(str(r["close_time"])).timestamp() - 5.0:
            continue  # the fit leaves out horizons that run into the close, where a favorite jumps to 99c; the review does the same so the two measure the same thing
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>=? AND ts<=? AND bid > 0 AND ask > 0 ORDER BY ts", (r["ticker"], r["ts"] - 20.0, ends + 20.0)).fetchall()
        if len(q) < 2:
            continue

        before = [x for x in q if x[0] <= r["ts"]]
        after = [x for x in q if x[0] <= ends]
        if not before or not after:
            continue
        got.append((move, _mid(after[-1], r["side"]) - _mid(before[-1], r["side"]), c.get("recalled"), r["action"]))
    out = [f"{lane}: {len(rows)} decisions with a saved forecast in the last {hours:g} h; {len(got)} have had their horizon pass"]
    if len(got) < 10:
        return [*out, "  too few to say anything yet"]
    xs, ys = [g[0] for g in got], [g[1] for g in got]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sx, sy = math.sqrt(sum((x - mx) ** 2 for x in xs)), math.sqrt(sum((y - my) ** 2 for y in ys))
    corr = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (sx * sy) if sx and sy else float("nan")
    out.append(f"  correlation of forecast with the move that followed {corr:+.3f}; forecast averaged {100 * mx:+.2f}c, the side's mid moved {100 * my:+.2f}c")
    out.append(f"  direction agreed on {statistics.mean((g[0] > 0) == (g[1] > 0) for g in got if g[1] != 0):.0%} of the decisions where it moved")
    for label, sel in (("bought", lambda g: g[3] == "bet"), ("passed", lambda g: g[3] != "bet")):
        sub = [g for g in got if sel(g)]
        if len(sub) >= 5:
            out.append(f"  {label:7} n={len(sub):4}  forecast {100 * statistics.mean(g[0] for g in sub):+5.2f}c  realized {100 * statistics.mean(g[1] for g in sub):+5.2f}c")
    return out



HORIZON = 5  # minutes: the horizon the forecast lane uses and the refit keeps current


def weights_path(s: Settings) -> str:
    return s.forecast_weights or f"{s.data_dir}/forecast_h{HORIZON}.json"


def wanted(s: Settings) -> bool:
    from .lanes import lane_specs

    return s.forecast_refit_hours > 0 and any(sp.estimator == "forecast" and sp.enabled for sp in lane_specs(s).values())


def start_refit(get_settings: Any) -> threading.Thread:
    """A thread that refits the weights every `forecast_refit_hours` while a forecast lane is on. The fit runs in its own process at low priority: it is a minute or two of pure Python
    and must not slow the recorder or the watchers in this one. A failed fit leaves the previous file in place."""

    def run() -> None:
        while True:
            s = get_settings()
            try:
                if wanted(s):
                    path = weights_path(s)
                    age = time.time() - os.path.getmtime(path) if os.path.exists(path) else float("inf")
                    if age >= s.forecast_refit_hours * 3600.0 - 60.0:
                        done = subprocess.run([sys.executable, "-m", "kalshi_moonshot", "forecast-fit", "--horizon", str(HORIZON), "--out", path],
                                              capture_output=True, text=True, timeout=1800, preexec_fn=lambda: os.nice(10))
                        if done.returncode:
                            log.warning("forecast refit failed: %s", (done.stderr or done.stdout)[-300:])
            except Exception:
                log.exception("forecast refit could not run")
            time.sleep(600.0)

    th = threading.Thread(target=run, name="forecast-refit", daemon=True)
    th.start()
    return th


def order_variants(ask: float, bid: float, d30: float, path: list[float], win: bool) -> dict[str, float | None]:
    """What each way of getting into one decision would have returned per dollar, held to the result. `path` is the side's ask at each quote after the decision, up to the end of the horizon.
    Taking the ask fills at once and pays the fee; a resting order pays none but fills only if the ask comes down to its price (the cautious rule: a trade through it is not counted).
    A price must stay on the exchange's scale, 0.0001 to 0.9999, and not above the ask; nothing else bounds it."""
    out: dict[str, float | None] = {"take the ask": ((1.0 if win else 0.0) - ask - fee_per_contract(ask)) / (ask + fee_per_contract(ask))}
    w = 1.0 if win else 0.0
    prices = {"rest at the bid": bid, "rest 1c under the ask": ask - 0.01}
    for c in (0.5, 1.0, 2.0):
        prices[f"rest {c:g} x last-30s move under the ask"] = ask - max(0.0001, c * abs(d30))
    for name, px in prices.items():
        px = round(px, 4)
        if px < forecast.PRICE_MIN or px > min(ask, forecast.PRICE_MAX):
            out[name] = None
        else:
            out[name] = (w - px) / px if any(x <= px + 1e-9 for x in path) else None
    return out


def review_orders(s: Settings, lane: str, hours: float = 24.0) -> list[str]:
    """For the forecast lane's settled decisions that bought: what each order style would have returned from the same moment (replayed on the tape)."""
    tape = db.connect(s.tape_path, "tape", shared=True)
    store = Store(s.db_path, s.exclude_versions)
    rows = store.db.execute("SELECT ts, ticker, side, outcome, context FROM decisions WHERE lane=? AND action='bet' AND ts>? AND outcome IN ('yes','no') AND context LIKE '{%' ORDER BY ts",
                            (lane, time.time() - hours * 3600.0)).fetchall()
    results: dict[str, list[tuple[Any, float | None]]] = defaultdict(list)
    used = 0
    for r in rows:
        try:
            horizon = float(json.loads(r["context"])["horizon"])
        except (ValueError, KeyError, TypeError):
            continue
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>=? AND ts<=? AND bid > 0 AND ask > 0 ORDER BY ts", (r["ticker"], r["ts"] - 45.0, r["ts"] + horizon * 60.0)).fetchall()
        before = [x for x in q if x[0] <= r["ts"]]
        if not before:
            continue
        yes = r["side"] == "yes"

        def sa(x: Any, yes: bool = yes) -> float:
            return x[2] if yes else 1.0 - x[1]

        def sb(x: Any, yes: bool = yes) -> float:
            return x[1] if yes else 1.0 - x[2]

        back = [x for x in before if x[0] <= r["ts"] - 30.0]
        d30 = sa(before[-1]) - sa(back[-1]) if back else 0.0
        used += 1
        got = order_variants(sa(before[-1]), sb(before[-1]), d30, [sa(x) for x in q if x[0] > r["ts"]], r["outcome"] == r["side"])
        for name, v in got.items():
            results[name].append((r["ts"], v))
    out = [f"{lane}: {len(rows)} settled buys in the last {hours:g} h, {used} with tape quotes"]
    if used < 10:
        return [*out, "  too few to say anything yet"]
    for name, vals in results.items():
        filled = [v for _, v in vals if v is not None]
        out.append(f"  {name:42} fills {len(filled) / len(vals):4.0%}  per fill {(statistics.mean(filled) if filled else float('nan')):+6.1%}  per decision {statistics.mean(v if v is not None else 0.0 for _, v in vals):+6.1%}")
    return out


def compare(s: Settings, lane: str = "crypto_favorite", days: float = 4.0, horizon: int = HORIZON, min_move: float = 0.005, out_path: str = "") -> list[str]:
    """Replay the forecaster against a lane's real, settled bets. For each day the weights are refit on windows that closed BEFORE that day, so a bet is never judged by a model that had seen it.
    Reports the lane's own result against what a forecaster-approved subset would have earned, and lists the calls that disagreed with the outcome (an approved loss, a rejected win) with
    the context at the time, including what the language-model lanes thought of the same market. `out_path` saves that list as JSON for the model to read."""
    from .factors import bet_roi

    tape = db.connect(s.tape_path, "tape", shared=True)
    store = Store(s.db_path, s.exclude_versions)
    rows = store.db.execute("SELECT ts, ticker, side, ask, outcome, exit_price, exit_reason FROM decisions WHERE lane=? AND mode='trade' AND action='bet' AND outcome IN ('yes','no') AND ts>? ORDER BY ts",
                            (lane, time.time() - days * 86400.0)).fetchall()
    models: dict[int, forecast.Weights | None] = {}
    seen: list[dict[str, Any]] = []
    for r in rows:
        day = int(r["ts"] // 86400) * 86400
        if day not in models:
            try:
                models[day] = forecast.Weights(build(s, horizon, before=float(day)))
            except Exception as exc:  # too little tape before that day
                log.info("compare: no model for the day starting %s (%s)", day, exc)
                models[day] = None
        w = models[day]
        ix = index_for_series(r["ticker"].split("-")[0])
        if w is None or ix is None:
            continue
        moves = forecast.slice_moves(tape, w, ix, r["side"], float(r["ts"]))
        roi = bet_roi(float(r["ask"]), r["side"], r["outcome"], r["exit_price"])
        if moves is None or roi is None:
            continue
        move, reg, memory = forecast.forecast_move(w, moves)
        move = max(forecast.PRICE_MIN - float(r["ask"]), min(move, forecast.PRICE_MAX - float(r["ask"])))
        llm = store.db.execute("SELECT lane, model_p FROM decisions WHERE lane IN ('llm_shape','llm_shape_hold') AND ticker=? AND ABS(ts-?)<240 ORDER BY ABS(ts-?) LIMIT 1", (r["ticker"], r["ts"], r["ts"])).fetchone()
        p_llm = None if not llm or llm["model_p"] is None else (float(llm["model_p"]) if r["side"] == "yes" else 1.0 - float(llm["model_p"]))
        seen.append({"ticker": r["ticker"], "side": r["side"], "ask": float(r["ask"]), "won": r["outcome"] == r["side"], "roi": roi, "exit": r["exit_reason"], "move": move, "regression": reg,
                     "recalled": memory, "slices": [round(z, 2) for z in moves], "llm_p": p_llm, "ts": float(r["ts"])})
    out = [f"{lane}: {len(rows)} settled real bets in the last {days:g} days; {len(seen)} replayed with a model fitted only on earlier days"]
    if len(seen) < 10:
        return [*out, "  too few to say anything yet"]

    def line(label: str, g: list[dict[str, Any]]) -> str:
        if not g:
            return f"  {label:34} none"
        return f"  {label:34} n={len(g):4}  won {sum(x['won'] for x in g) / len(g):4.0%}  average return {statistics.mean(x['roi'] for x in g):+6.1%}  total {sum(x['roi'] for x in g):+6.2f} (per $1 each)"

    yes = [x for x in seen if x["move"] >= min_move]
    no = [x for x in seen if x["move"] < min_move]
    out += [line("what the lane bought", seen), line(f"forecaster approves (move >= {100 * min_move:.1f}c)", yes), line("forecaster would skip", no)]
    losses, wins = [x for x in seen if not x["won"]], [x for x in seen if x["won"]]
    out.append(f"  of {len(losses)} losses it would have skipped {sum(x['move'] < min_move for x in losses)}; of {len(wins)} wins it would have skipped {sum(x['move'] < min_move for x in wins)}")
    withllm = [x for x in seen if x["llm_p"] is not None]
    if len(withllm) >= 10:
        agree = [x for x in withllm if (x["llm_p"] > x["ask"]) == x["won"]]
        out.append(f"  language-model lanes also rated {len(withllm)} of these; their call (chance above the ask = buy) matched the outcome on {len(agree) / len(withllm):.0%}")
    misses = [x for x in seen if (x["move"] >= min_move and not x["won"]) or (x["move"] < min_move and x["won"])]
    out.append(f"\n  calls that disagreed with the outcome ({len(misses)}): the context to learn from")
    for x in sorted(misses, key=lambda y: -abs(y["move"]))[:12]:
        kind = "took a loss" if not x["won"] else "skipped a win"
        out.append(f"    {x['ticker'][:26]:26} {x['side']:3} @{x['ask']:.2f} {kind:13} forecast {100 * x['move']:+5.2f}c (regression {100 * x['regression']:+5.2f}, past moments "
                   f"{'n/a' if x['recalled'] is None else format(100 * x['recalled'], '+5.2f')}) slices {x['slices']}" + ("" if x["llm_p"] is None else f" model said {x['llm_p']:.2f}"))
    if out_path:
        with open(out_path, "w") as f:
            json.dump({"made_at": time.time(), "lane": lane, "misses": misses}, f)
        out.append(f"  saved to {out_path}")
    return out
