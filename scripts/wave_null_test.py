"""NULL TEST of the wave-top buy (2026-11-23; user: "we need to check the logic for buy and sell on the wave step by step, because something is flawed"). Step 1 of the buy: does the detector (wavetop.signal_at on the 60 s average, minutes 2 to 10) fire on real waves, or on anything with the same volatility? On every eligible window of the study cache (crypto and commodity), the detector is run on the real spot path and on SHUFFLED copies (the one-second changes of the whole path in random order, so the volatility and the distribution of moves stay and the order, which is where a wave lives, goes; the strike keeps its offset from the spot at the open). The fire rate (a first signal in minutes 2 to 10, no price band) and the side mix are compared. A detector that reads waves fires more on the real paths than on the shuffled ones. Read-only; a lead only.
    .venv/bin/python scripts/wave_null_test.py [CACHE_DIR]"""

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import study_cache as sc  # noqa: E402

from kalshi_moonshot import wavetop as wt  # noqa: E402

LO, HI, QTY = 0.18, 0.55, 25
ENTRY_FROM, ENTRY_TO, LOOK = 120, 600, 120
import numpy as np  # noqa: E402

cache = sc.Cache(sys.argv[1] if len(sys.argv) > 1 else "job-results/20261123-022002-76512")
t_start = time.time()
rng = np.random.default_rng(11)
SHUF = 2
ia = 3700


def first_signal(sp, strike):
    A = wt.average(sp)
    ua = wt.typical_minute(A, ia)
    if ua <= 0:
        return None
    z = wt.Zig(ia - wt.LOOK, A[ia - wt.LOOK], ua, 0.0, wt.LEG_A)
    for k in range(ia - wt.LOOK + 1, ia + ENTRY_TO + 1):
        z.step(k, A[k])
        t = k - ia
        if t < ENTRY_FROM:
            continue
        wv, _ = wt.signal_at(z, A[k], sp[k], strike, ua, sp[ia])
        if wv is not None:
            return t, wv.side
    return None


stats = {}
for w in sorted(cache.windows, key=lambda x: (x["close"], x["ticker"])):
    if w["result"] not in ("yes", "no") or w["strike"] is None:
        continue
    close = int(w["close"])
    a = close - 900
    T, V = cache.ticks(w["index"])
    sp = wt.grid(T, V, a - 3700, close)
    if None in sp[ia - 3660 : ia + ENTRY_TO + 1]:
        continue
    sp = sp[: ia + ENTRY_TO + 1]
    strike = float(w["strike"])
    kind = w["kind"]
    d = stats.setdefault(kind, {"n": 0, "real": 0, "shuf": 0, "shuf_n": 0, "real_t": [], "shuf_t": [], "real_yes": 0, "shuf_yes": 0})
    real = first_signal(sp, strike)
    d["n"] += 1
    if real:
        d["real"] += 1
        d["real_t"].append(real[0])
        d["real_yes"] += real[1] == "yes"
    arr = np.array(sp, dtype=float)
    inc = np.diff(arr)
    for _ in range(SHUF):
        p = rng.permutation(inc)
        sh = np.concatenate(([arr[0]], arr[0] + np.cumsum(p)))
        strike2 = strike - arr[ia] + sh[ia]
        got = first_signal(sh.tolist(), strike2)
        d["shuf_n"] += 1
        if got:
            d["shuf"] += 1
            d["shuf_t"].append(got[0])
            d["shuf_yes"] += got[1] == "yes"

print(f"detector fire rate (a first signal in minutes 2 to 10, no price band); shuffled = the same path's one-second changes in random order ({SHUF} shuffles per window)")
for kind, d in sorted(stats.items()) + [("all", {k: (sum(x[k] for x in stats.values()) if not isinstance(stats[next(iter(stats))][k], list) else sum((x[k] for x in stats.values()), [])) for k in next(iter(stats.values()))})]:
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else float("nan")
    print(f"{kind:10s} windows {d['n']:5d} | real fires {d['real'] / d['n']:.3f} (median minute {med(d['real_t']) / 60:.1f}, YES share {d['real_yes'] / max(1, d['real']):.2f}) | shuffled fires {d['shuf'] / d['shuf_n']:.3f} (median minute {med(d['shuf_t']) / 60:.1f}, YES share {d['shuf_yes'] / max(1, d['shuf']):.2f})")
print(f"\n{time.time() - t_start:.0f} s")
