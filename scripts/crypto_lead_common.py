"""Loader shared by the crypto_lead_*.py studies: per-second yes bid/ask of every Crypto Leader window (5 coin markets, one event) from the tape archive, with the settled result.
Window = 900 s before close. A quote is carried forward from its last update (a second before the first quote is nan). Window needs all 5 markets, a result for each, and a first quote at least 10 minutes before the close (earlier seconds are nan and skipped by the studies).
Only days from 2026-11-09 (project rule: nothing before Monday 11-09 in studies)."""
import calendar
import csv
import glob
from collections import defaultdict
from datetime import datetime

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = "data/tape_archive"
COINS = ("BTC", "ETH", "SOL", "XRP", "HYPE")
FEE = 0.07
RES_FILE = "studies/results/2026-11-19-crypto-lead-results.csv"


def fee(p):
    return FEE * p * (1 - p)


def read_quotes(substr):
    """{ticker: (ts, bid, ask)} for tickers containing substr, all archived days from 11-09."""
    parts = []
    for f in sorted(glob.glob(ROOT + "/quotes/day=*/part-0.parquet")):
        t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
        t = t.filter(pc.match_substring(t["ticker"], substr))
        parts.append(t.to_pandas())
    import pandas as pd
    df = pd.concat(parts).sort_values(["ticker", "ts"])
    out = {}
    for tk, g in df.groupby("ticker", sort=False):
        out[tk] = (g["ts"].to_numpy(), g["bid"].to_numpy(), g["ask"].to_numpy())
    return out


def second_grid(q, close):
    """bid, ask arrays of 900 seconds (index 0 = close-900) from a quote triple."""
    ts, b, a = q
    ok = (a > 0) & (a <= 1) & (b >= 0) & (a >= b) & (ts < close)
    ts, b, a = ts[ok], b[ok], a[ok]
    if len(ts) == 0 or ts[0] > close - 600:
        return None
    secs = np.arange(close - 900, close)
    at = np.searchsorted(ts, secs, side="right") - 1
    have = at >= 0
    ix = np.maximum(at, 0)
    return np.where(have, b[ix], np.nan), np.where(have, a[ix], np.nan)


def load_windows():
    """list of dict(event, close, day, bid[5,900], ask[5,900], win[5]) in COINS order."""
    res = {}
    for r in csv.reader(open(RES_FILE)):
        if len(r) == 3 and r[2] in ("yes", "no"):
            res[r[0]] = (int(r[1]), r[2])
    qs = read_quotes("CRYPTOLEAD")
    ev = defaultdict(dict)
    for tk, q in qs.items():
        ev[tk.rsplit("-", 1)[0]][tk.rsplit("-", 1)[1]] = (tk, q)
    out = []
    for e, d in ev.items():
        if set(d) != set(COINS) or any(d[c][0] not in res for c in COINS):
            continue
        close = res[d[COINS[0]][0]][0]
        grids = [second_grid(d[c][1], close) for c in COINS]
        if any(g is None for g in grids):
            continue
        win = np.array([res[d[c][0]][1] == "yes" for c in COINS])
        if win.sum() != 1:
            continue
        day = datetime.utcfromtimestamp(close - 1).strftime("%Y-%m-%d")
        out.append(dict(event=e, close=close, day=day, bid=np.array([g[0] for g in grids]), ask=np.array([g[1] for g in grids]), win=win))
    out.sort(key=lambda w: w["close"])
    return out
