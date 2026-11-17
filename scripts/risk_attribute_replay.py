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
    .venv/bin/python scripts/risk_attribute_replay.py report    # read it and print the tables (against the result)
    .venv/bin/python scripts/risk_attribute_replay.py held      # the model's call bought and held to settlement (one fee), against no signal
    .venv/bin/python scripts/risk_attribute_replay.py rest      # the same bets bought by resting a bid for a minute instead of taking the ask
    .venv/bin/python scripts/risk_attribute_replay.py moves     # against the price's move over the next 2 and 5 minutes, as round trips after the spread and the fee
"""
import bisect
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
VIEWS = f"{ROOT}/replay_views.csv"
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
    with open(OUT, "w", newline="") as fh, open(VIEWS, "w", newline="") as vh:
        w = csv.writer(fh)
        w.writerow(["ticker", "ts", "age", "won", "bid", "ask", *NAMES])
        vw = csv.writer(vh)
        vw.writerow(["ticker", "ts", "p5", "p15", "p60", "jump60", "eff15"])  # jump60: the index's largest one-minute move in the hour ending at the moment, in its typical minutes (a chart with jumps or dives in it). eff15: its net move over the last 15 minutes over the path it travelled, minute by minute, signed (+1 a straight climb, -1 a straight fall, near 0 waves that went nowhere)  # the target's place in the index's range over the 5, 15 and 60 minutes ending at the moment (0 = at the bottom of the view, 1 = at the top; under 0 or over 1 = outside it): the views the user reads by hand (2026-11-17)
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
                    hi = bisect.bisect_right(ts, now)
                    places = []
                    for m in (5, 15, 60):
                        seg = vs[bisect.bisect_right(ts, now - 60.0 * m):hi]
                        places.append(round((strike - min(seg)) / (max(seg) - min(seg)), 4) if seg and max(seg) > min(seg) else 0.5)
                    mk = [riskscore._at(ts, vs, now - 60.0 * k_) for k_ in range(60, -1, -1)]
                    steps = [math.log(b_ / a_) if a_ and b_ else 0.0 for a_, b_ in zip(mk, mk[1:])]
                    jump = max(abs(v) for v in steps) / unit
                    path = sum(abs(v) for v in steps[-15:])
                    eff = sum(steps[-15:]) / path if path > 0 else 0.0
                    vw.writerow([ticker, int(now), *places, round(jump, 3), round(eff, 3)])
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


def moves() -> None:
    """Round trips instead of results: does an attribute say where the PRICE goes over the next minutes, and does acting on it pay after the spread and the fee on both legs?"""
    from kalshi_moonshot.strategy import fee_per_contract

    rows = list(csv.reader(open(OUT)))[1:]
    rows.sort(key=lambda r: (r[0], int(r[1])))
    tick = np.array([r[0] for r in rows])
    ts = np.array([int(r[1]) for r in rows])
    age = np.array([float(r[2]) for r in rows])
    bid = np.array([float(r[4]) for r in rows])
    ask = np.array([float(r[5]) for r in rows])
    X = np.array([[float(v) for v in r[6:]] for r in rows])
    mid = (bid + ask) / 2.0
    day = np.array([datetime.datetime.fromtimestamp(t, ET).strftime("%m-%d") for t in ts])
    days = sorted(set(day))[1:]  # the first ET day is a part day
    at = {(a, b): i for i, (a, b) in enumerate(zip(tick, ts))}
    zone = np.where(np.abs(X[:, 1]) < 0.5, 0, np.where(np.abs(X[:, 1]) < 1.5, 1, 2))
    zname = ["on the line (cushion under 0.5)", "fair cushion (0.5 to 1.5)", "wide cushion (over 1.5)"]
    print(f"window: 2026-11-09 on, ET days {', '.join(days)}; {len(rows)} readings on {len(set(tick))} markets; whole market, no time or price gate")
    print("a round trip buys at the ask and sells at the bid some minutes later; the fee is charged on both legs; results are per dollar staked")
    print("settings (the top tenth of predicted moves, one entry per market) were fixed before the run\n")
    for minutes in (2, 5):
        h = minutes * 60
        nxt = np.array([at.get((a, b + h), -1) for a, b in zip(tick, ts)])
        ok = (nxt >= 0) & (bid > 0) & (ask > 0) & (ask < 1)
        dm = np.where(ok, mid[nxt] - mid, 0.0)
        print(f"===== {minutes} minutes ahead: {int(ok.sum())} readings with a mark {minutes} minutes later")
        print(f"A. each attribute against the mid's move over the next {minutes} minutes (correlation), by day")
        print(f"{'attribute':13s} {'all days':>9s} | " + " ".join(f"{d:>6s}" for d in days) + " | days + / -")
        for i, n in enumerate(NAMES):
            per = [_corr(X[ok & (day == d), i], dm[ok & (day == d)]) for d in days]
            print(f"{n:13s} {_corr(X[ok, i], dm[ok]):+9.3f} | " + " ".join(f"{c:+6.3f}" for c in per) + f" | {sum(c > 0.01 for c in per)} / {sum(c < -0.01 for c in per)}")

        def net(i: int, side: int) -> float:
            """One round trip from reading i: side +1 buys YES at its ask and sells at its bid; -1 buys NO at (1 - YES bid) and sells at (1 - YES ask)."""
            j = nxt[i]
            buy, sell = (ask[i], bid[j]) if side > 0 else (1.0 - bid[i], 1.0 - ask[j])
            if not (0.0 < buy < 1.0) or sell <= 0.0:
                return float("nan")
            cost = buy + fee_per_contract(buy)
            return (sell - fee_per_contract(sell) - cost) / cost

        print(f"\nB. walk-forward: a linear fit of the {minutes}-minute move on the fifteen attributes, from every earlier day; on the next day the first moment in each market whose predicted move is in the top tenth (by size, either way; the cut is the earlier days') is bought on the side the fit points to and sold {minutes} minutes later")
        print(f"{'day':6s} {'trips':>6s} {'ahead':>6s} {'behind':>6s} {'net per $':>10s} {'net on $5':>10s} | any side, any moment (the cost of a round trip with no signal)")
        Xi = np.c_[np.ones(len(X)), X]
        all_trips = []
        for k in range(2, len(days)):
            tr, te = ok & np.isin(day, days[:k]), ok & (day == days[k])
            if tr.sum() < 1000 or not te.sum():
                continue
            w = np.linalg.solve(Xi[tr].T @ Xi[tr] + 1e-3 * np.eye(Xi.shape[1]), Xi[tr].T @ dm[tr])
            cut = np.quantile(np.abs(Xi[tr] @ w), 0.9)
            pred = Xi @ w
            done, trips = set(), []
            for i in np.flatnonzero(te):
                if tick[i] in done or abs(pred[i]) < cut:
                    continue
                v = net(i, 1 if pred[i] > 0 else -1)
                if v == v:
                    done.add(tick[i])
                    trips.append((v, zone[i], age[i], ask[i] if pred[i] > 0 else 1.0 - bid[i], days[k]))
            rng = np.random.default_rng(7)
            base = [net(i, 1 if rng.random() < 0.5 else -1) for i in rng.choice(np.flatnonzero(te), size=min(3000, int(te.sum())), replace=False)]
            base = [b for b in base if b == b]
            all_trips += trips
            v = np.array([t[0] for t in trips]) if trips else np.array([0.0])
            print(f"{days[k]:6s} {len(trips):6d} {int((v > 0).sum()):6d} {int((v <= 0).sum()):6d} {v.mean():+10.4f} {5 * v.sum():+10.2f} | {np.mean(base):+.4f}")
        if all_trips:
            v = np.array([t[0] for t in all_trips])
            print(f"{'all':6s} {len(v):6d} {int((v > 0).sum()):6d} {int((v <= 0).sum()):6d} {v.mean():+10.4f} {5 * v.sum():+10.2f}")
            print("by curve shape at the buy (the cushion zone the score itself uses):")
            for z in range(3):
                u = np.array([t[0] for t in all_trips if t[1] == z])
                if len(u):
                    print(f"  {zname[z]:34s} trips {len(u):5d} | ahead {int((u > 0).sum()):5d} | behind {int((u <= 0).sum()):5d} | net per $ {u.mean():+.4f} | on $5 stakes {5 * u.sum():+.2f}")
            print("by the price paid:")
            for lo, hi in ((0, 0.10), (0.10, 0.35), (0.35, 0.65), (0.65, 0.90), (0.90, 1.01)):
                sel = [t for t in all_trips if lo <= t[3] < hi]
                u = np.array([t[0] for t in sel])
                if len(u):
                    pd_ = defaultdict(float)
                    for t in sel:
                        pd_[t[4]] += 5 * t[0]
                    print(f"  paid {lo:.2f} to {min(hi, 1.0):.2f}                  trips {len(u):5d} | ahead {int((u > 0).sum()):5d} | behind {int((u <= 0).sum()):5d} | net per $ {u.mean():+.4f} | middle trip {np.median(u):+.4f} | on $5 stakes {5 * u.sum():+.2f} | days ahead {sum(v > 0 for v in pd_.values())} of {len(pd_)}")
            print("by when in the market the buy was:")
            for lo, hi in ((0, 180), (180, 360), (360, 540), (540, 900)):
                u = np.array([t[0] for t in all_trips if lo <= t[2] < hi])
                if len(u):
                    print(f"  {lo // 60:2d} to {hi // 60:2d} min in                    trips {len(u):5d} | ahead {int((u > 0).sum()):5d} | behind {int((u <= 0).sum()):5d} | net per $ {u.mean():+.4f} | on $5 stakes {5 * u.sum():+.2f}")
        print()


def held() -> None:
    """Buy on the model's call and hold to settlement: one spread crossed, one fee. Two calls, each walk-forward (fit on every earlier day, bought on the next): the RESULT fit (a side whose fitted chance is over its ask plus the fee) and the MOVE fit (the side the 5-minute move fit points to)."""
    from kalshi_moonshot.strategy import fee_per_contract

    rows = list(csv.reader(open(OUT)))[1:]
    rows.sort(key=lambda r: (r[0], int(r[1])))
    tick = np.array([r[0] for r in rows])
    ts = np.array([int(r[1]) for r in rows])
    age = np.array([float(r[2]) for r in rows])
    y = np.array([float(r[3]) for r in rows])
    bid = np.array([float(r[4]) for r in rows])
    ask = np.array([float(r[5]) for r in rows])
    X = np.array([[float(v) for v in r[6:]] for r in rows])
    mid = (bid + ask) / 2.0
    day = np.array([datetime.datetime.fromtimestamp(t, ET).strftime("%m-%d") for t in ts])
    days = sorted(set(day))[1:]
    at = {(a, b): i for i, (a, b) in enumerate(zip(tick, ts))}
    nxt = np.array([at.get((a, b + 300), -1) for a, b in zip(tick, ts)])
    dm = np.where(nxt >= 0, mid[nxt] - mid, 0.0)
    zone = np.where(np.abs(X[:, 1]) < 0.5, 0, np.where(np.abs(X[:, 1]) < 1.5, 1, 2))
    zname = ["on the line (cushion under 0.5)", "fair cushion (0.5 to 1.5)", "wide cushion (over 1.5)"]
    ok = (bid > 0) & (ask > 0) & (ask < 1) & (bid < 1)
    fee = np.vectorize(fee_per_contract)
    cost_yes, cost_no = ask + fee(np.clip(ask, 0.01, 0.99)), (1.0 - bid) + fee(np.clip(1.0 - bid, 0.01, 0.99))

    def net(i: int, side: int) -> float:
        c = cost_yes[i] if side > 0 else cost_no[i]
        won = y[i] if side > 0 else 1.0 - y[i]
        return (won - c) / c

    def show(name: str, trips: list) -> None:
        def line(label: str, sel: list) -> None:
            u = np.array([t[0] for t in sel])
            if not len(u):
                return
            pd_ = defaultdict(float)
            for t in sel:
                pd_[t[4]] += 5 * t[0]
            print(f"  {label:34s} bets {len(u):5d} | won {int((u > 0).sum()):5d} | lost {int((u <= 0).sum()):5d} | net per $ {u.mean():+.4f} | on $5 stakes {5 * u.sum():+9.2f} | days ahead {sum(v > 0 for v in pd_.values())} of {len(pd_)}")

        print(f"--- {name}")
        for d in days:
            line(d, [t for t in trips if t[4] == d])
        line("all days", trips)
        print("  by curve shape at the buy (the cushion zone the score itself uses):")
        for z in range(3):
            line("  " + zname[z], [t for t in trips if t[1] == z])
        print("  by the price paid:")
        for lo, hi in ((0, 0.10), (0.10, 0.35), (0.35, 0.65), (0.65, 0.90), (0.90, 1.01)):
            line(f"  paid {lo:.2f} to {min(hi, 1.0):.2f}", [t for t in trips if lo <= t[3] < hi])
        print("  by when in the market the buy was:")
        for lo, hi in ((0, 180), (180, 360), (360, 540), (540, 720), (720, 900)):
            line(f"  {lo // 60} to {hi // 60} min in", [t for t in trips if lo <= t[2] < hi])
        print()

    print(f"window: 2026-11-09 on, ET days {', '.join(days)}; {len(rows)} readings on {len(set(tick))} markets; whole market, no time or price gate")
    print("a bet buys a side at its ask and holds it to settlement; the fee is charged once; results are per dollar staked; one bet per market (the first moment that qualifies)")
    print("settings (the top tenth of the call's size, the cut taken from the earlier days) were fixed before the run\n")
    Xi = np.c_[np.ones(len(X)), X]
    res_trips, mov_trips, base_trips, fav_trips = [], [], [], []
    rng = np.random.default_rng(7)
    for k in range(2, len(days)):
        tr, te = ok & np.isin(day, days[:k]), ok & (day == days[k])
        if tr.sum() < 1000 or not te.sum():
            continue
        # the RESULT fit: the fitted chance against what the side costs
        w = _fit(X[tr], y[tr])
        p = _sig(X @ w)
        edge_yes, edge_no = p - cost_yes, (1.0 - p) - cost_no
        best = np.maximum(edge_yes, edge_no)
        cut = np.quantile(best[tr], 0.9)
        # the MOVE fit: the predicted 5-minute move of the mid
        trm = tr & (nxt >= 0)
        wm = np.linalg.solve(Xi[trm].T @ Xi[trm] + 1e-3 * np.eye(Xi.shape[1]), Xi[trm].T @ dm[trm])
        pred = Xi @ wm
        cutm = np.quantile(np.abs(pred[trm]), 0.9)
        done_r, done_m, done_b = set(), set(), set()
        idx = np.flatnonzero(te)
        for i in idx:
            if tick[i] not in done_r and best[i] >= cut and best[i] > 0:
                side = 1 if edge_yes[i] >= edge_no[i] else -1
                done_r.add(tick[i])
                res_trips.append((net(i, side), zone[i], age[i], cost_yes[i] if side > 0 else cost_no[i], days[k]))
            if tick[i] not in done_m and abs(pred[i]) >= cutm:
                side = 1 if pred[i] > 0 else -1
                done_m.add(tick[i])
                mov_trips.append((net(i, side), zone[i], age[i], cost_yes[i] if side > 0 else cost_no[i], days[k]))
        for i in rng.permutation(idx):  # no signal: one random moment per market, a random side
            if tick[i] not in done_b:
                side = 1 if rng.random() < 0.5 else -1
                done_b.add(tick[i])
                base_trips.append((net(i, side), zone[i], age[i], cost_yes[i] if side > 0 else cost_no[i], days[k]))
                fs = 1 if mid[i] >= 0.5 else -1  # the same moment, the favorite
                fav_trips.append((net(i, fs), zone[i], age[i], cost_yes[i] if fs > 0 else cost_no[i], days[k]))
    show("no signal: one random moment per market, a random side, held", base_trips)
    show("no signal: the same moments, the favorite, held", fav_trips)
    show("the RESULT fit's call, held", res_trips)
    show("the MOVE fit's call (5 minutes), held", mov_trips)


def _quotes_15m() -> dict:
    """ticker -> (times, YES bid, YES ask) arrays, every two-sided quote on the archive, in time order."""
    parts = defaultdict(list)
    for f in sorted(glob.glob(f"{ROOT}/quotes/day=*/part-0.parquet")):
        t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
        t = t.filter(pc.and_(pc.match_substring(t["ticker"], "15M-"), pc.and_(pc.greater(t["bid"], 0), pc.greater(t["ask"], 0))))
        tk = np.array(t["ticker"].to_pylist())
        ts, bid, ask = t["ts"].to_numpy(), t["bid"].to_numpy(), t["ask"].to_numpy()
        o = np.argsort(tk, kind="stable")
        tk, ts, bid, ask = tk[o], ts[o], bid[o], ask[o]
        cuts = np.flatnonzero(tk[1:] != tk[:-1]) + 1
        for a, b in zip(np.r_[0, cuts], np.r_[cuts, len(tk)]):
            parts[tk[a]].append((ts[a:b], bid[a:b], ask[a:b]))
    out = {}
    for tk, ps in parts.items():
        ts = np.concatenate([p[0] for p in ps])
        o = np.argsort(ts, kind="stable")
        out[tk] = (ts[o], np.concatenate([p[1] for p in ps])[o], np.concatenate([p[2] for p in ps])[o])
    return out


REST_SECONDS = 60.0  # how long the bid rests before it is pulled; fixed before the run


def rest() -> None:
    """The same bets, bought two ways: taking the ask, or resting a bid at the side's own best bid for REST_SECONDS and holding what fills. The archive has quotes, not the order book or the trades, so a fill is read two ways:
      through  the side's ask came down to our bid or under: the price traded through us, we were filled for certain (and the price was moving against us when it happened)
      cleared  the side's best bid fell under our bid: the level we were resting on was used up or pulled. Filled if it was used up; an upper bound.
    The fee: none on a resting buy as the bot treats it (runner.py: "as a maker and is charged no taker fee"), and with the full fee as the worst case."""
    from kalshi_moonshot.strategy import fee_per_contract

    rows = list(csv.reader(open(OUT)))[1:]
    rows.sort(key=lambda r: (r[0], int(r[1])))
    tick = np.array([r[0] for r in rows])
    ts = np.array([int(r[1]) for r in rows])
    age = np.array([float(r[2]) for r in rows])
    y = np.array([float(r[3]) for r in rows])
    bid = np.array([float(r[4]) for r in rows])
    ask = np.array([float(r[5]) for r in rows])
    X = np.array([[float(v) for v in r[6:]] for r in rows])
    mid = (bid + ask) / 2.0
    day = np.array([datetime.datetime.fromtimestamp(t, ET).strftime("%m-%d") for t in ts])
    days = sorted(set(day))[1:]
    at = {(a, b): i for i, (a, b) in enumerate(zip(tick, ts))}
    nxt = np.array([at.get((a, b + 300), -1) for a, b in zip(tick, ts)])
    dm = np.where(nxt >= 0, mid[nxt] - mid, 0.0)
    zone = np.where(np.abs(X[:, 1]) < 0.5, 0, np.where(np.abs(X[:, 1]) < 1.5, 1, 2))
    zname = ["on the line (cushion under 0.5)", "fair cushion (0.5 to 1.5)", "wide cushion (over 1.5)"]
    ok = (bid > 0) & (ask > 0) & (ask < 1) & (bid < 1) & (ask > bid) & (age <= 900.0 - REST_SECONDS - 30.0)
    quotes = _quotes_15m()

    def trip(i: int, side: int) -> tuple:
        """(take net, our resting price, filled through, filled cleared, the side won, the price paid taking)."""
        qt, qb, qa = quotes[tick[i]]
        a, b = np.searchsorted(qt, ts[i], side="right"), np.searchsorted(qt, ts[i] + REST_SECONDS, side="right")
        won = y[i] if side > 0 else 1.0 - y[i]
        if side > 0:
            price, take = bid[i], ask[i]
            through, cleared = bool((qa[a:b] <= price + 1e-9).any()), bool((qb[a:b] < price - 1e-9).any())
        else:
            price, take = 1.0 - ask[i], 1.0 - bid[i]
            through, cleared = bool((qb[a:b] >= ask[i] - 1e-9).any()), bool((qa[a:b] > ask[i] + 1e-9).any())
        c = take + fee_per_contract(take)
        return (won - c) / c, price, through, cleared or through, won, take

    def show(name: str, bets: list) -> None:
        def line(label: str, sel: list) -> None:
            if not sel:
                return
            n = len(sel)
            take = np.array([b[0][0] for b in sel])
            out = [f"  {label:34s} bets {n:5d} | taking the ask {take.mean():+.4f} per $, ${5 * take.sum():+9.2f}"]
            for key, k in (("through", 2), ("cleared", 3)):
                f = [b[0] for b in sel if b[0][k]]
                if not f:
                    out.append(f"{key}: none filled")
                    continue
                free = np.array([(g[4] - g[1]) / g[1] for g in f])
                paid = np.array([(g[4] - g[1] - fee_per_contract(g[1])) / (g[1] + fee_per_contract(g[1])) for g in f])
                missed = np.array([b[0][0] for b in sel if not b[0][k]])
                out.append(f"{key}: filled {100 * len(f) / n:4.1f}%, won {100 * np.mean([g[4] for g in f]):4.1f}%, {free.mean():+.4f} per $ no fee (${5 * free.sum():+9.2f}), {paid.mean():+.4f} with the fee (${5 * paid.sum():+9.2f}); the unfilled, had they been taken: {missed.mean() if len(missed) else 0.0:+.4f}")
            print(" | ".join(out))

        print(f"--- {name}")
        for d in days:
            line(d, [b for b in bets if b[3] == d])
        line("all days", bets)
        print("  by curve shape at the bid (the cushion zone the score itself uses):")
        for z in range(3):
            line("  " + zname[z], [b for b in bets if b[1] == z])
        print("  by the price of the side (taking):")
        for lo, hi in ((0, 0.35), (0.35, 0.65), (0.65, 0.90), (0.90, 1.01)):
            line(f"  {lo:.2f} to {min(hi, 1.0):.2f}", [b for b in bets if lo <= b[0][5] < hi])
        print("  by when in the market:")
        for lo, hi in ((0, 180), (180, 360), (360, 540), (540, 900)):
            line(f"  {lo // 60} to {hi // 60} min in", [b for b in bets if lo <= b[2] < hi])
        print()

    print(f"window: 2026-11-09 on, bets on ET days {', '.join(days[2:])}; whole market, no time or price gate; one bet per market")
    print(f"each bet is bought two ways: taking the ask and holding, or resting a bid at the side's best bid for {REST_SECONDS:.0f} s and holding what fills. Per dollar staked; dollar totals on $5 stakes")
    print("the rest time and the bid's price (the side's own best bid) were fixed before the run\n")
    Xi = np.c_[np.ones(len(X)), X]
    fav_bets, mov_bets = [], []
    rng = np.random.default_rng(7)
    for k in range(2, len(days)):
        tr, te = ok & np.isin(day, days[:k]), ok & (day == days[k])
        if tr.sum() < 1000 or not te.sum():
            continue
        trm = tr & (nxt >= 0)
        wm = np.linalg.solve(Xi[trm].T @ Xi[trm] + 1e-3 * np.eye(Xi.shape[1]), Xi[trm].T @ dm[trm])
        pred = Xi @ wm
        cutm = np.quantile(np.abs(pred[trm]), 0.9)
        done_m, done_b = set(), set()
        idx = np.flatnonzero(te)
        for i in idx:
            if tick[i] not in done_m and abs(pred[i]) >= cutm:
                done_m.add(tick[i])
                mov_bets.append((trip(i, 1 if pred[i] > 0 else -1), zone[i], age[i], days[k]))
        for i in rng.permutation(idx):
            if tick[i] not in done_b:
                done_b.add(tick[i])
                fav_bets.append((trip(i, 1 if mid[i] >= 0.5 else -1), zone[i], age[i], days[k]))
    show("no model: the favorite at a random moment", fav_bets)
    show("the 5-minute move fit's call", mov_bets)


if __name__ == "__main__":
    if sys.argv[1:] == ["rest"]:
        rest()
        sys.exit(0)
    if sys.argv[1:] == ["held"]:
        held()
        sys.exit(0)
    if sys.argv[1:] == ["moves"]:
        moves()
        sys.exit(0)
    (build if sys.argv[1:] == ["build"] else report)()
