"""The perp's chance, read as the perp_worth step reads it, on every commodity market around the 132 live bets with step notes (2026-11-21): the own-history a level bar is cut from.
For each commodity series, every market closing 2026-11-18 12:00 UTC to 2026-11-21 00:00 UTC, every 10 s from minute 1 to 20 s before the close: each side whose ask is a price the live lanes buy (0.60-0.92, a quote at most 10 s old) gets curvevalue.read(perp ticks, the perp's open tick, side, time left, "900").p, the number variants.curve_edge_now gives the step. Readings only, no bet and no outcome.
Writes perp_chance_history.json.gz ({series: [[ts, chance], ...]}) to DATA_DIR. Run: SPOT=0 scripts/cloud_job.sh scripts/perp_chance_history.py"""
import bisect, datetime, gzip, json, multiprocessing, os
from pathlib import Path
from kalshi_moonshot import config, curvevalue, db
from kalshi_moonshot.perpref import index_for_series as perp_index

U = lambda *a: datetime.datetime(*a, tzinfo=datetime.timezone.utc).timestamp()
A, Z = U(2026, 11, 18, 12), U(2026, 11, 21)
s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
jobs = {}
for w in st.execute("SELECT ticker, series, close_ts FROM windows WHERE close_ts BETWEEN ? AND ? ORDER BY close_ts", (A, Z)).fetchall():
    if w["series"].startswith("KXINX") or not perp_index(w["series"]): continue
    jobs.setdefault(w["series"], []).append((w["ticker"], float(w["close_ts"])))


def build(job):
    series, ws = job
    tape = db.connect(config.reload().tape_path, "tape", shared=True)
    ix = perp_index(series)
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, A - 2000, Z + 5)).fetchall()
    T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
    out = []
    for ticker, close in ws:
        opened = close - 900.0
        i = bisect.bisect_left(T, opened - 5.0)
        if i >= len(T) or T[i] > opened + 20.0: continue
        ref = V[i]
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, opened, close)).fetchall()
        if len(q) < 20: continue
        qt = [float(r[0]) for r in q]
        sc = 100.0 if max(float(r[2]) for r in q) > 1.5 else 1.0
        t = opened + 60.0
        while t <= close - 20.0:
            j = bisect.bisect_right(qt, t) - 1
            if j >= 0 and t - qt[j] <= 10.0:
                yb, ya = float(q[j][1]) / sc, float(q[j][2]) / sc
                a0, a1 = bisect.bisect_left(T, t - 1000.0), bisect.bisect_right(T, t)
                for side, ask in (("yes", ya), ("no", 1.0 - yb)):
                    if 0.60 <= ask <= 0.92:
                        c = curvevalue.read(T[a0:a1], V[a0:a1], t, ref, side, close - t, "900")
                        if c is not None: out.append((t, round(c.p, 4)))
            t += 10.0
    return series, out


H = {}
with multiprocessing.get_context("fork").Pool(4) as pool:
    for series, out in pool.imap_unordered(build, list(jobs.items())):
        H[series] = sorted(out)
        print(series, len(out), flush=True)
with gzip.open(Path(os.environ.get("DATA_DIR", "/data")) / "perp_chance_history.json.gz", "wt") as f: json.dump(H, f)
