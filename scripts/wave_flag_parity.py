"""PARITY CHECK of the buy step `wave_flag` (src/kalshi_moonshot/waveflag.py) against the lab's `flag_fires` (scripts/wave_flag_lab.py), 2026-11-23. Read-only; a check, not a study.

On real windows of the study cache (the lab's own filters: settled, a strike, the spot gridded with no gap from 3,660 s before the open to minute 10, quotes covering the market), for M = 1 and M = 2:
  lab    every fire second and side in minutes 2 to 10: the lab's `flag_fires` (taken verbatim from the lab file by its syntax tree) asked again from one second after each fire (its ENTRY_FROM moved), so the first answer is the lab's own and the rest are every later fire;
  step   waveflag.read on the cache's ticks at every second from 120 to 600 (hold 0: the flag in its confirmation second only), the way the live step reads the tick memory;
  live   on a subset, the buy step itself (buychain.STEPS["wave_flag"]) with the ticks in ticks.history and the target from waveflag.target (crypto: the market's strike; commodity: the perp at the open), both sides, every second, hold 0.
Any second where they differ is printed with the window.
    PYTHONPATH=src .venv/bin/python scripts/wave_flag_parity.py [CACHE_DIR] [LAB_FILE] [N_WINDOWS] [N_LIVE]"""

import ast
import math
import multiprocessing
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))
import study_cache as sc  # noqa: E402

from kalshi_moonshot import wavetop as wt  # noqa: E402
from kalshi_moonshot import waveflag  # noqa: E402

CACHE = sys.argv[1] if len(sys.argv) > 1 else "/Users/johnjosef/Projects/kalshi-moonshot/job-results/20261123-022002-76512"
LAB = Path(sys.argv[2]) if len(sys.argv) > 2 else next(p for p in (HERE / "wave_flag_lab.py", Path("/Users/johnjosef/Projects/kalshi-moonshot/scripts/wave_flag_lab.py")) if p.exists())
N = int(sys.argv[3]) if len(sys.argv) > 3 else 400
N_LIVE = int(sys.argv[4]) if len(sys.argv) > 4 else 40
IA = 3700


def lab_namespace() -> dict:
    """The lab's constants and its two functions, by its syntax tree (the lab runs its whole study on import)."""
    tree = ast.parse(LAB.read_text())
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("flag_fires", "sigma10")]
    want = {"Z", "T_FRAC", "M_SET", "WIN_LEGS", "ENTRY_FROM", "ENTRY_TO", "LO", "HI"}
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Tuple) and {getattr(e, "id", None) for e in t.elts} & want or getattr(t, "id", None) in want for t in n.targets):
            keep.insert(0, n)
    ns: dict = {"math": math}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(LAB), "exec"), ns)
    return ns


NS = lab_namespace()
ENTRY_FROM, ENTRY_TO = NS["ENTRY_FROM"], NS["ENTRY_TO"]
Z, TURN, WIN = NS["Z"], NS["T_FRAC"], NS["WIN_LEGS"]


def eligible(cache):
    """The lab's window loop, its filters, in its order; yields (w, spc, strike, sigma10)."""
    for w in sorted(cache.windows, key=lambda x: (x["close"], x["ticker"])):
        if w["result"] not in ("yes", "no") or w["strike"] is None:
            continue
        close = int(w["close"])
        a = close - 900
        T, V = cache.ticks(w["index"])
        sp = wt.grid(T, V, a - 3700, close)
        if None in sp[IA - 3660 : IA + ENTRY_TO + 1]:
            continue
        qt, qb, qa = cache.quotes(w)
        if len(qt) < 100:
            continue
        bids, asks = wt.grid(qt, qb, a, close), wt.grid(qt, qa, a, close)
        qfirst = max(0, int(qt[0]) - a + 1)
        if qfirst > ENTRY_TO or None in bids[qfirst:] or None in asks[qfirst:]:
            continue
        spc, prev = [], next(float(x) for x in sp if x is not None)
        for x in sp[: IA + ENTRY_TO + 1]:
            prev = float(x) if x is not None else prev
            spc.append(prev)
        sg = NS["sigma10"](spc, IA)
        if sg <= 0:
            continue
        yield w, spc, float(w["strike"]), sg


def lab_all(spc, strike, sg, m):
    """Every lab fire for M=m in [ENTRY_FROM, ENTRY_TO]: {second: side}; first = the lab's own answer."""
    out, t0 = {}, ENTRY_FROM
    while t0 <= ENTRY_TO:
        NS["ENTRY_FROM"] = t0
        got = NS["flag_fires"](spc, IA, strike, sg, Ms=(m,))[m]
        if got is None:
            break
        out[got[0]] = got[1]
        t0 = got[0] + 1
    NS["ENTRY_FROM"] = ENTRY_FROM
    return out


_C = {}


def check(job):
    i, ticker = job
    cache = _C["c"]
    w = next(x for x in cache.windows if x["ticker"] == ticker)
    close = int(w["close"])
    opened = close - 900
    T, V = cache.ticks(w["index"])
    import bisect

    lo, hi = max(0, bisect.bisect_right(T, opened - waveflag.LEAD) - 1), bisect.bisect_right(T, opened + ENTRY_TO + 1)  # from the last tick at or before the path's start, as read_index gives it
    Ts, Vs = T[lo:hi], V[lo:hi]
    spc, strike, sg = _C["arr"][ticker]
    res = {"ticker": ticker, "kind": w["kind"], "first_ok": {}, "lab": {}, "step": {}, "diff": {}}
    for m in (1, 2):
        lab = lab_all(spc, strike, sg, m)
        first_lab = NS["flag_fires"](spc, IA, strike, sg, Ms=(m,))[m]
        step = {}
        for t in range(ENTRY_FROM, ENTRY_TO + 1):
            now = opened + t + 0.5
            f, _ = waveflag.read(Ts, Vs, opened, now, strike, Z, TURN, m, float(WIN), 0.0)
            if f is not None:
                step[t] = f.side
        first_step = min(step.items()) if step else None
        res["first_ok"][m] = (first_lab is None and first_step is None) or (first_lab is not None and first_step == tuple(first_lab))
        res["lab"][m], res["step"][m] = lab, step
        res["diff"][m] = sorted(set(lab.items()) ^ set(step.items()))
    return res


def live_check(w, spc, strike, sg):
    """The buy step itself, from the tick memory, both sides, every second, hold 0."""
    from dataclasses import replace

    from kalshi_moonshot import buychain, lanes, ticks
    from kalshi_moonshot.strategy import LongShot

    cache = _C["c"]
    close = int(w["close"])
    opened = close - 900
    T, V = cache.ticks(w["index"])
    import bisect

    lo, hi = bisect.bisect_left(T, opened - 8 * 3600), bisect.bisect_right(T, close)  # the bot warms eight hours
    ticks.history.clear()
    ticks.strikes._v.clear()
    waveflag._memo.clear()
    ticks.history.warm(w["index"], list(zip(T[lo:hi], V[lo:hi])), T[lo] - 1.0)
    if w["kalshi_strike"]:
        ticks.strikes.note(w["ticker"], float(w["kalshi_strike"]))
    base = lanes.LaneSpec("wf", "ladder", "wf", (0.0, 1.0), (0.0, 1.0), 1.0, 0.0, sides=("yes", "no"))
    out = {}
    for m in (1, 2):
        spec = replace(base, wave_flag_legs=m)
        s = types.SimpleNamespace(entry_min_gap_seconds=0.0)
        lab = lab_all(spc, strike, sg, m)
        got = {}
        tgt = waveflag.target(w["series"], w["index"], float(opened), ticks.strikes.get(w["ticker"]))[0]
        for t in range(ENTRY_FROM, ENTRY_TO + 1):
            now = opened + t + 0.5
            left = (close - now) / 3600.0
            yes_no = []
            for side in ("yes", "no"):
                sh = LongShot(w["ticker"], "t", "", 0.3, 0.31, left, "2099-01-01T00:00:00Z", left, lane="wf", series=w["series"], side=side)
                yes_no.append(buychain.STEPS["wave_flag"](buychain.Ctx(s, spec, now=now), sh).ok)
            if yes_no[0] and yes_no[1]:
                raise AssertionError("both sides said yes")
            if any(yes_no):
                got[t] = "yes" if yes_no[0] else "no"
        out[m] = (sorted(set(lab.items()) ^ set(got.items())), tgt)
    return out


def main():
    t0 = time.time()
    cache = sc.Cache(CACHE)
    rows = list(eligible(cache))
    n = min(N, len(rows))
    pick = [rows[int(j * len(rows) / n)] for j in range(n)]
    _C["c"] = cache
    _C["arr"] = {w["ticker"]: (spc, strike, sg) for w, spc, strike, sg in pick}
    print(f"lab file {LAB}; cache {CACHE}; eligible windows {len(rows)}; compared {len(pick)} spread evenly over them ({sum(w['kind'] == 'crypto' for w, *_ in pick)} crypto, {sum(w['kind'] == 'commodity' for w, *_ in pick)} commodity)")
    print(f"lab settings Z {Z} turn {TURN} window {WIN} entry {ENTRY_FROM}-{ENTRY_TO} s")
    with multiprocessing.get_context("fork").Pool(6) as pool:
        got = pool.map(check, [(i, w["ticker"]) for i, (w, *_r) in enumerate(pick)])
    for m in (1, 2):
        first_bad = [r for r in got if not r["first_ok"][m]]
        sec_bad = [r for r in got if r["diff"][m]]
        fires = sum(1 for r in got if r["lab"][m])
        events = sum(len(r["lab"][m]) for r in got)
        print(f"M={m}: windows {len(got)}, lab first fire in {fires} ({fires / len(got):.3f}), lab fire seconds in all {events}; first fire and side mismatches {len(first_bad)}; per-second mismatches (481 s x 2 sides each) {len(sec_bad)}")
        for r in sec_bad[:10]:
            print(f"   {r['ticker']} {r['kind']}: {r['diff'][m][:6]}")
    print(f"core check {time.time() - t0:.0f} s")
    t1 = time.time()
    live = pick[:: max(1, len(pick) // N_LIVE)][:N_LIVE]
    bad, tgt_bad, fired = 0, 0, {1: 0, 2: 0}
    for w, spc, strike, sg in live:
        out = live_check(w, spc, strike, sg)
        for m in (1, 2):
            diff, tgt = out[m]
            fired[m] += bool(lab_all(spc, strike, sg, m))
            if tgt is None or abs(tgt - strike) > 1e-12:
                tgt_bad += 1
                print(f"   target differs {w['ticker']}: step {tgt} lab {strike}")
            if diff:
                bad += 1
                print(f"   live step differs {w['ticker']} M={m}: {diff[:6]}")
    print(f"live step through buychain on {len(live)} windows (both sides, every second 120-600, M=1 and M=2; windows with a lab fire M=1 {fired[1]}, M=2 {fired[2]}): mismatches {bad}, target mismatches {tgt_bad}; {time.time() - t1:.0f} s")


if __name__ == "__main__":
    main()
