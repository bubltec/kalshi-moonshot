"""Does a faster price show a crypto spike before the Kalshi quote moves? (2026-11-08, user: "we want to check if we can read the spike because Kalshi is slow"; "they both have to be on sockets and pooling to the same memory because we can't have it making a request")

Question: Hyperliquid's perpetual price is already on the tape one row per second (tape.fut_1s, from the futflow.py socket) for BTC, ETH, SOL, XRP and DOGE. When it jumps (a 10-second move of at least 3x its own trailing 10-minute one-second RMS x sqrt(10), the book_leads_spot_study.py threshold; events in a market 30 s apart; 60-840 s into the window), what do the Kalshi quote and the settlement index do over the next 1 to 20 seconds, in the jump's direction?
  Kalshi: the YES mid after the jump less its value at the jump, signed with the jump (cents; positive = the quote keeps moving with the jump, i.e. it is slow), and the share of events where the mid has moved 3 cents or more with the jump within 1, 3, 5, 10 and 20 s (the price tick is structural).
  Index: the settlement index (BRTI or the coin's RTI) move since the jump in its own typical-minute units scaled to the horizon (as book_leads_spot_study.py), signed with the jump.
  Already priced: the Kalshi mid and index moves in the 10 s up to the jump, same sign, so a quote that had already moved is told apart from one that has not.
The baseline is the same measure after ordinary moves (10-second move between 1x and 2x the threshold's RMS unit), so drift is not counted as lead.
Reported: n, mean +- standard error, two halves, the last 300 s only (where the exits act), and spot path-efficiency terciles. The Hyperliquid second is the exchange's trade time, the Kalshi and index times are the tape's receive time: a clock offset of a second or two between them is not removed and is the study's main caveat.
Not covered: what a sale at the shown bid would net after the spread and fee (this only asks whether there is time to read the spike first); a faster feed's cost in memory (one pair of arrays per symbol in ticks.TickHistory).

    scripts/cloud.sh run scripts/spike_lead_study.py
"""

import bisect
import calendar
import collections
import itertools
import math
import os
import time

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 2, 0, 0, 0))
COINS = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL", "KXXRP15M": "XRP", "KXDOGE15M": "DOGE"}
if os.environ.get("COIN"):
    COINS = {k: v for k, v in COINS.items() if v == os.environ["COIN"]}
HORIZONS = (1, 3, 5, 10, 20)
events = []  # dicts


def grid(T, V, a, b):
    out, i, n = [], 0, len(T)
    for t in range(a, b + 1):
        while i + 1 < n and T[i + 1] <= t:
            i += 1
        out.append(V[i] if n and T[i] <= t else 0.0)
    return out


for ser, coin in COINS.items():
    ix = variants.crypto_index(ser)
    wins = st.db.execute("SELECT ticker,close_ts FROM windows WHERE series=? AND close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (ser, t0 + 7200)).fetchall()
    if not wins:
        continue
    lo, hi = int(float(wins[0]["close_ts"])) - 4500, int(float(wins[-1]["close_ts"])) + 5
    hl = tape.execute("SELECT ts,px FROM fut_1s WHERE symbol=? AND ts>=? AND ts<=? AND px>0 ORDER BY ts", (coin, lo, hi)).fetchall()
    idx = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>=? AND ts<=? AND value>0 ORDER BY ts", (ix, lo, hi)).fetchall()
    if len(hl) < 1000 or len(idx) < 1000:
        continue
    H = grid([float(x[0]) for x in hl], [float(x[1]) for x in hl], lo, hi)
    I = grid([float(x[0]) for x in idx], [float(x[1]) for x in idx], lo, hi)
    hlr = [0.0] + [math.log(b / a) if a > 0 and b > 0 else 0.0 for a, b in itertools.pairwise(H)]
    ir = [0.0] + [math.log(b / a) if a > 0 and b > 0 else 0.0 for a, b in itertools.pairwise(I)]
    pre_h, pre_i = list(itertools.accumulate([x * x for x in hlr], initial=0.0)), list(itertools.accumulate([x * x for x in ir], initial=0.0))
    for w in wins:
        close = float(w["close_ts"])
        opened = close - 900.0
        q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 30, close + 1)).fetchall()
        if len(q) < 60:
            continue
        qt = [float(x[0]) for x in q]
        qm = [(float(x[1]) + float(x[2])) / 2.0 for x in q]

        def mid(t, qt=qt, qm=qm):
            j = bisect.bisect_right(qt, t) - 1
            return qm[j] if j >= 0 and t - qt[j] < 20 else None

        last_ev = -1e9
        for e in range(60, 841):
            t = opened + e
            k = int(t) - lo
            if k < 700 or k + 25 >= len(H) or t - last_ev < 30:
                continue
            m10 = math.log(H[k] / H[k - 10]) if H[k] > 0 and H[k - 10] > 0 else 0.0
            rms_h = math.sqrt((pre_h[k] - pre_h[k - 600]) / 600.0)
            rms_i = math.sqrt((pre_i[k] - pre_i[k - 600]) / 600.0)
            if rms_h <= 0 or rms_i <= 0 or m10 == 0.0:
                continue
            unit10 = rms_h * math.sqrt(10.0)
            ratio = abs(m10) / unit10
            kind = "spike" if ratio >= 3.0 else "ordinary" if 1.0 <= ratio < 2.0 else None
            if kind is None:
                continue
            d = 1.0 if m10 > 0 else -1.0
            m0 = mid(t)
            mpre = mid(t - 10.0)
            if m0 is None or mpre is None:
                continue
            ev = {"kind": kind, "ts": t, "left": close - t, "pre_q": (m0 - mpre) * d * 100.0, "pre_i": math.log(I[k] / I[k - 10]) * d / (rms_i * math.sqrt(10.0)) if I[k] > 0 and I[k - 10] > 0 else 0.0}
            ok = True
            for h in HORIZONS:
                mh = mid(t + h)
                if mh is None or I[k + h] <= 0 or I[k] <= 0:
                    ok = False
                    break
                ev[f"q{h}"] = (mh - m0) * d * 100.0
                ev[f"i{h}"] = math.log(I[k + h] / I[k]) * d / (rms_i * math.sqrt(h))
            if not ok:
                continue
            for h in HORIZONS:
                ev[f"r{h}"] = ev[f"q{h}"] >= 3.0
            seg = [x for x in I[k - 300 : k + 1] if x > 0]
            path = sum(abs(math.log(b / a)) for a, b in itertools.pairwise(seg))
            ev["eff"] = abs(math.log(seg[-1] / seg[0])) / path if len(seg) > 10 and path > 0 else None
            events.append(ev)
            last_ev = t
    print(ser, "events so far", len(events), time.strftime("%H:%M:%S"), flush=True)

mid_t = sorted(e["ts"] for e in events)[len(events) // 2]
effs = sorted(e["eff"] for e in events if e["eff"] is not None)
lo3, hi3 = effs[len(effs) // 3], effs[2 * len(effs) // 3]
shape = lambda e: "unknown" if e is None else "choppy" if e <= lo3 else "straight" if e >= hi3 else "middle"


def mean_se(xs):
    n = len(xs)
    if n < 2:
        return 0.0, 0.0
    m = sum(xs) / n
    return m, (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5 / n**0.5


def line(label, sel):
    if not sel:
        print(f"   {label:34s} n=0")
        return
    n = len(sel)
    pq = mean_se([e["pre_q"] for e in sel])
    pi = mean_se([e["pre_i"] for e in sel])
    qs = "  ".join(f"{h}s {mean_se([e[f'q{h}'] for e in sel])[0]:+.2f}c" for h in HORIZONS)
    ss = "  ".join(f"{h}s {100 * sum(e[f'r{h}'] for e in sel) / n:3.0f}%" for h in HORIZONS)
    isx = "  ".join(f"{h}s {mean_se([e[f'i{h}'] for e in sel])[0]:+.2f}" for h in HORIZONS)
    print(f"   {label:34s} n={n:5d} | already priced: quote {pq[0]:+.2f}c index {pi[0]:+.2f}u\n      Kalshi mid after, with the jump: {qs}\n      share moved 3c+ with the jump:   {ss}\n      index after, units:               {isx}")


print("\nevents", len(events), "spike", sum(e["kind"] == "spike" for e in events), "ordinary", sum(e["kind"] == "ordinary" for e in events))
for kind in ("spike", "ordinary"):
    sel = [e for e in events if e["kind"] == kind]
    print(f"\n{kind} events (Hyperliquid 10 s move {'>= 3x' if kind == 'spike' else '1x-2x'} its own unit):")
    line("all", sel)
    line("first half", [e for e in sel if e["ts"] <= mid_t])
    line("second half", [e for e in sel if e["ts"] > mid_t])
    line("last 300 s only", [e for e in sel if e["left"] <= 300])
    for sh in ("choppy", "middle", "straight"):
        line(f"curve shape {sh}", [e for e in sel if shape(e["eff"]) == sh])
print("DONE")
