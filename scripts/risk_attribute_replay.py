"""Replay the archived tape on the Mac and read the risk score's fifteen attributes over every 15-minute market, whole (no time or price gate).

Question (user, 2026-11-17): "I just want to get an idea of what's useful and what's not. See if there's a signal"; "we could rebuild the attributes locally by playing the tape".
One question on the same moments: at each 30 s mark of each market, does an attribute say anything about the result that Kalshi's own price does not, and does it say the same thing day after day?

Reads (nothing on the host):
  data/tape_archive/quotes/day=*/part-0.parquet        the archive's quotes
  data/tape_archive/index_ticks/day=*/part-0.parquet   the archive's index ticks
  data/tape_archive/windows.csv                        ticker,close_ts,result,strike (from the host's windows table)
The attributes are riskscore.features(whole=True): the code the live recorder runs (riskrecord.py). The target is the index's 60 s average before the open (scenario.target), as live.
Window: 2026-11-09 on (the logic was rewritten that Monday; nothing before it is counted).

    .venv/bin/python scripts/risk_attribute_replay.py build     # write data/tape_archive/replay_readings.csv
    .venv/bin/python scripts/risk_attribute_replay.py report    # read it and print the tables
"""
import csv
import datetime
import glob
import math
import sys
from collections import defaultdict
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

from kalshi_moonshot import riskscore, scenario, variants

ROOT = "data/tape_archive"
OUT = f"{ROOT}/replay_readings.csv"
ET = ZoneInfo("America/New_York")
NAMES = riskscore.NAMES


def build() -> None:
    wins = {}
    for r in csv.reader(open(f"{ROOT}/windows.csv")):
        if len(r) >= 3 and r[2] in ("yes", "no"):
            wins[r[0]] = (float(r[1]), r[2] == "yes")
    series = sorted({t.split("-")[0] for t in wins})
    index_of = {s: variants.spot_index(s, "", 0.0) for s in series}
    need = {v for v in index_of.values() if v}
    print(f"{len(wins)} settled markets on {len(series)} series; indexes {sorted(need)}", flush=True)

    ix_parts = defaultdict(list)
    for f in sorted(glob.glob(f"{ROOT}/index_ticks/day=*/part-0.parquet")):
        t = pq.read_table(f, columns=["ts", "index_id", "value"])
        t = t.filter(pc.and_(pc.is_in(t["index_id"], value_set=__import__("pyarrow").array(sorted(need))), pc.greater(t["value"], 0)))
        ids = np.array(t["index_id"].to_pylist())
        ts, vs = t["ts"].to_numpy(), t["value"].to_numpy()
        for i in need:
            m = ids == i
            ix_parts[i].append((ts[m], vs[m]))
        print(f"  index ticks {f.split('/')[-2]}: {len(ts)}", flush=True)
    index = {}
    for i, parts in ix_parts.items():
        ts, vs = np.concatenate([p[0] for p in parts]), np.concatenate([p[1] for p in parts])
        o = np.argsort(ts, kind="stable")
        index[i] = (ts[o], vs[o])

    q_parts = defaultdict(list)
    for f in sorted(glob.glob(f"{ROOT}/quotes/day=*/part-0.parquet")):
        t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
        t = t.filter(pc.and_(pc.match_substring(t["ticker"], "15M-"), pc.and_(pc.greater(t["bid"], 0), pc.greater(t["ask"], 0))))  # a one-sided quote is no mid (MidHistory.note)
        tk = np.array(t["ticker"].to_pylist())
        ts, bid, ask = t["ts"].to_numpy(), t["bid"].to_numpy(), t["ask"].to_numpy()
        o = np.argsort(tk, kind="stable")
        tk, ts, bid, ask = tk[o], ts[o], bid[o], ask[o]
        cuts = np.flatnonzero(tk[1:] != tk[:-1]) + 1
        for a, b in zip(np.r_[0, cuts], np.r_[cuts, len(tk)]):
            q_parts[tk[a]].append((ts[a:b], bid[a:b], ask[a:b]))
        print(f"  quotes {f.split('/')[-2]}: {len(ts)}", flush=True)
    quotes = {}
    for tk, parts in q_parts.items():
        ts = np.concatenate([p[0] for p in parts])
        o = np.argsort(ts, kind="stable")
        ts, bid, ask = ts[o], np.concatenate([p[1] for p in parts])[o], np.concatenate([p[2] for p in parts])[o]
        last = np.r_[np.floor(ts[1:]) != np.floor(ts[:-1]), True]  # one row a second, the second's latest quote: as the memory tape keeps it
        quotes[tk] = (ts[last], bid[last], ask[last])
    by_series_close = {(t.split("-")[0], c): t for t, (c, _) in wins.items()}

    n_rows = n_mk = 0
    skipped = defaultdict(int)
    with open(OUT, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "ts", "age", "won", "bid", "ask", *NAMES])
        for k, (ticker, (close, won)) in enumerate(sorted(wins.items(), key=lambda kv: kv[1][0])):
            ser = ticker.split("-")[0]
            ix = index_of.get(ser)
            opened = close - 900.0
            if not ix or ix not in index:
                skipped["no index on the tape"] += 1
                continue
            if ticker not in quotes:
                skipped["no quotes"] += 1
                continue
            its, ivs = index[ix]
            a, b = np.searchsorted(its, opened - 6400.0), np.searchsorted(its, close, side="right")
            ts, vs = its[a:b].tolist(), ivs[a:b].tolist()
            unit = riskscore.unit_before(ts, vs, opened)
            strike = scenario.target(ts, vs, opened)
            if unit is None or not strike:
                skipped["index history does not reach an hour before the open"] += 1
                continue
            qt, qb, qa = quotes[ticker]
            mids = list(zip(qt.tolist(), ((qb + qa) / 2.0).tolist()))
            hour = []
            for back in range(1, 5):
                tk = by_series_close.get((ser, close - 900.0 * back))
                if tk in quotes:
                    pt, pb, pa = quotes[tk]
                    hour += list(zip(pt.tolist(), ((pb + pa) / 2.0).tolist()))
            hour.sort()
            prev = riskscore.previous(ts, vs, [m for m in hour if opened - 3600.0 < m[0] <= opened], opened, unit)
            got = 0
            now = opened + 30.0
            while now < close:
                x = riskscore.features(ts, vs, now, opened, strike, mids, prev, whole=True)
                if x is not None:
                    j = int(np.searchsorted(qt, now, side="right")) - 1
                    w.writerow([ticker, int(now), int(now - opened), int(won), round(float(qb[j]), 3), round(float(qa[j]), 3), *[round(v, 4) for v in x]])
                    got += 1
                now += 30.0
            n_rows += got
            n_mk += 1 if got else 0
            if not got:
                skipped["no readable mark"] += 1
            if k % 1000 == 0:
                print(f"  {k} markets walked, {n_rows} readings", flush=True)
    print(f"wrote {n_rows} readings on {n_mk} markets to {OUT}; skipped: {dict(skipped)}")


def _sig(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _fit(X, y, ridge=1e-3):
    w = np.zeros(X.shape[1])
    for _ in range(30):
        p = _sig(X @ w)
        g = X.T @ (y - p) - ridge * w
        H = (X * (p * (1 - p))[:, None]).T @ X + ridge * np.eye(X.shape[1])
        d = np.linalg.solve(H, g)
        w += d
        if np.abs(d).sum() < 1e-7:
            break
    return w


def _loss(X, y, w):
    p = np.clip(_sig(X @ w), 1e-6, 1 - 1e-6)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def _corr(a, b):
    return float(np.corrcoef(a, b)[0, 1]) if a.std() > 0 and b.std() > 0 else 0.0


def report() -> None:
    rows = list(csv.reader(open(OUT)))[1:]
    tick = np.array([r[0] for r in rows])
    ts = np.array([float(r[1]) for r in rows])
    age = np.array([float(r[2]) for r in rows])
    y = np.array([float(r[3]) for r in rows])
    X = np.array([[float(v) for v in r[6:]] for r in rows])
    mid = _sig(X[:, 0])
    resid = y - mid
    day = np.array([datetime.datetime.fromtimestamp(t, ET).strftime("%m-%d") for t in ts])
    days = sorted(set(day))
    lane = (age >= 60) & (mid > 0.03) & (mid < 0.97)  # the moments a lane's reading is taken at
    print(f"window: 2026-11-09 on, ET days {', '.join(days)}; {len(rows)} readings (one per market per 30 s, whole market, no time or price gate) on {len(set(tick))} markets; {int(lane.sum())} of them are moments a lane reads")
    print("readings inside one market are not independent: read the n as markets, not readings\n")

    print("1. each attribute against what the price missed (correlation with YES won - Kalshi's mid), by day. A sign that holds across the days is a signal; one that flips is noise or a market that changed.")
    print(f"{'attribute':13s} {'spread':>7s} {'all days':>9s} | " + " ".join(f"{d:>6s}" for d in days) + " | days + / -")
    for i, n in enumerate(NAMES):
        per = [_corr(X[day == d, i], resid[day == d]) for d in days]
        print(f"{n:13s} {X[:, i].std():7.3f} {_corr(X[:, i], resid):+9.3f} | " + " ".join(f"{c:+6.3f}" for c in per) + f" | {sum(c > 0.01 for c in per)} / {sum(c < -0.01 for c in per)}")

    print("\n2. the same, by where in the market the moment is (all days)")
    phases = [("first 3 min", age < 180), ("3 to 6", (age >= 180) & (age < 360)), ("6 to 9", (age >= 360) & (age < 540)), ("9 to 12", (age >= 540) & (age < 720)), ("last 3 min", age >= 720)]
    print(f"{'attribute':13s} " + " ".join(f"{p:>12s}" for p, _ in phases))
    print(f"{'(readings)':13s} " + " ".join(f"{int(m.sum()):12d}" for _, m in phases))
    for i, n in enumerate(NAMES):
        print(f"{n:13s} " + " ".join(f"{_corr(X[m, i], resid[m]):+12.3f}" for _, m in phases))

    print("\n3. the same, by Kalshi's price for YES (all days)")
    zones = [("under 0.10", mid < 0.10), ("0.10-0.35", (mid >= 0.10) & (mid < 0.35)), ("0.35-0.65", (mid >= 0.35) & (mid < 0.65)), ("0.65-0.90", (mid >= 0.65) & (mid < 0.90)), ("0.90 and up", mid >= 0.90)]
    print(f"{'attribute':13s} " + " ".join(f"{p:>12s}" for p, _ in zones))
    print(f"{'(readings)':13s} " + " ".join(f"{int(m.sum()):12d}" for _, m in zones))
    print(f"{'YES won-mid':13s} " + " ".join(f"{resid[m].mean():+12.3f}" for _, m in zones))
    for i, n in enumerate(NAMES):
        print(f"{n:13s} " + " ".join(f"{_corr(X[m, i], resid[m]):+12.3f}" for _, m in zones))

    print("\n4. a fit of all fifteen on each day alone (the weight each attribute gets next to the others). A weight that keeps its sign every day is doing the same job every day.")
    print(f"{'attribute':13s} | " + " ".join(f"{d:>6s}" for d in days) + " | days + / -")
    per_day = {d: _fit(X[(day == d) & lane], y[(day == d) & lane]) for d in days}
    for i, n in enumerate(NAMES):
        ws = [per_day[d][i] for d in days]
        print(f"{n:13s} | " + " ".join(f"{v:+6.2f}" for v in ws) + f" | {sum(v > 0 for v in ws)} / {sum(v < 0 for v in ws)}")

    print("\n5. walk-forward on the moments a lane reads: fit on every earlier day, score the next day. Log-loss, lower is better. 'price' is Kalshi's mid as it stands.")
    used = [i for i, n in enumerate(NAMES) if n in riskscore.USED]
    tot = defaultdict(float)
    cnt = 0
    drop = defaultdict(float)
    print(f"{'day':6s} {'readings':>9s} {'price':>8s} {'all 15':>8s} {'the 7':>8s}")
    for k in range(2, len(days)):
        tr, te = lane & np.isin(day, days[:k]), lane & (day == days[k])
        if not te.sum():
            continue
        l_mid = _loss(X[te][:, :1], y[te], np.array([1.0]))
        l_all = _loss(X[te], y[te], _fit(X[tr], y[tr]))
        l_use = _loss(X[te][:, used], y[te], _fit(X[tr][:, used], y[tr]))
        print(f"{days[k]:6s} {int(te.sum()):9d} {l_mid:8.4f} {l_all:8.4f} {l_use:8.4f}")
        n = int(te.sum())
        cnt += n
        tot["price"] += l_mid * n
        tot["all"] += l_all * n
        tot["used"] += l_use * n
        for i in range(1, len(NAMES)):
            keep = [j for j in range(len(NAMES)) if j != i]
            drop[i] += (_loss(X[te][:, keep], y[te], _fit(X[tr][:, keep], y[tr])) - l_all) * n
    if cnt:
        print(f"{'all':6s} {cnt:9d} {tot['price'] / cnt:8.4f} {tot['all'] / cnt:8.4f} {tot['used'] / cnt:8.4f}")
        print("\n6. what leaving one attribute out of the fifteen does to that walk-forward loss (x 10,000). Positive: the fit is worse without it (it helps). Negative: the fit is better without it (it hurts).")
        for i in sorted(drop, key=lambda i: -drop[i]):
            print(f"  {NAMES[i]:13s} {1e4 * drop[i] / cnt:+7.2f}")


if __name__ == "__main__":
    (build if sys.argv[1:] == ["build"] else report)()
