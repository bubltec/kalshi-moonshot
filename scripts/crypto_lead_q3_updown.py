"""Q3: how the Crypto Leader market relates to the up/down crypto markets (KXBTC15M ... KXHYPE15M, the lane's markets).
Same close time, same five coins. Up/down result (yes = up) from windows.csv. A "wave" = all five coins settled the same way (all up or all down); "near wave" = four of five. Questions: (a) how often waves happen in these windows; (b) does the leader-market quote itself differ in waves (leader mid at 3 and 5 minutes, i.e. does a wave make one coin pull away early); (c) who leads in an up wave and in a down wave, by curve (rank of the coin's up/down mid typical-minute move, measured on the PRIOR window = known before the window, and in the window itself = after the fact); coin names only label; (d) hedge check: the five leader YES contracts pay exactly 1 in total whatever the direction, so it is direction-neutral; the table shows the cost to hold all five (sum of asks) in wave and non-wave windows. Findings only, no rule. n and days printed. Marks are 60 s apart; windows are the independent unit.
Run: PYTHONPATH=scripts .venv/bin/python scripts/crypto_lead_q3_updown.py > studies/results/2026-11-19-crypto-lead-q3-updown.txt"""
import csv
import re
from datetime import datetime, timedelta
from collections import Counter

import numpy as np

from crypto_lead_common import COINS, load_windows, read_quotes

W = load_windows()
res = {}
for r in csv.reader(open("data/tape_archive/windows.csv")):
    mt = re.match(r"KX(\w+?)15M-(\d\d[A-Z]{3}\d{6})-", r[0]) if r and r[0].startswith("KX") else None
    if mt and r[2] in ("yes", "no") and mt.group(1) in COINS:
        res[(mt.group(1), mt.group(2))] = r[2] == "yes"
ud = read_quotes("15M-")
ud = {k: v for k, v in ud.items() if re.match(r"KX(BTC|ETH|SOL|XRP|HYPE)15M-", k)}
byk = {}
for tk, q in ud.items():
    mt = re.match(r"KX(\w+?)15M-(\d\d[A-Z]{3}\d{6})-", tk)
    byk[(mt.group(1), mt.group(2))] = q


def rms(q, close, start, end):
    """typical minute move (rms 1 s move of the mid x sqrt 60) of an up/down market between two epoch seconds, nan if thin."""
    ts, b, a = q
    ok = (a > 0) & (a <= 1) & (a >= b)
    ts, m = ts[ok], ((a + b) / 2)[ok]
    secs = np.arange(start, end)
    at = np.searchsorted(ts, secs, side="right") - 1
    if len(ts) < 20 or ts[0] > start + 120:
        return np.nan
    x = m[np.maximum(at, 0)]
    d = np.diff(x)
    return float(np.sqrt((d * d).mean()) * np.sqrt(60))


rows = []
for w in W:
    ev = w["event"].split("-")[-1]
    ups = [res.get((c, ev)) for c in COINS]
    if any(u is None for u in ups):
        continue
    nu = sum(ups)
    pev = (datetime.strptime(ev, "%y%b%d%H%M") - timedelta(minutes=15)).strftime("%y%b%d%H%M").upper()
    pri = [rms(byk[(c, pev)], w["close"] - 900, w["close"] - 1800, w["close"] - 900) if (c, pev) in byk else np.nan for c in COINS]
    cur = [rms(byk[(c, ev)], w["close"], w["close"] - 900, w["close"]) if (c, ev) in byk else np.nan for c in COINS]
    rows.append(dict(w=w, nu=nu, pri=np.array(pri), cur=np.array(cur)))
print(f"leader windows with up/down results for all 5 coins: n={len(rows)} of {len(W)}, {rows[0]['w']['day']} to {rows[-1]['w']['day']}")
c = Counter(r["nu"] for r in rows)
print("coins up (of 5) per window:", {k: c[k] for k in sorted(c)})
wave = [r for r in rows if r["nu"] in (0, 5)]
near = [r for r in rows if r["nu"] in (1, 4)]
mixed = [r for r in rows if r["nu"] in (2, 3)]
print(f"waves (5 same) n={len(wave)} ({len(wave)/len(rows):.1%}); near waves (4 of 5) n={len(near)}; mixed n={len(mixed)}. up waves {sum(r['nu']==5 for r in rows)}, down waves {sum(r['nu']==0 for r in rows)}")


def lm(w, k):
    m = (w["bid"][:, k] + w["ask"][:, k]) / 2
    return np.nan if np.isnan(m).any() else m.max()


print("\n(b) leader-market quote by window type (median leader mid, median sum of asks)")
for name, grp in (("wave", wave), ("near wave", near), ("mixed", mixed)):
    out = []
    for k in (180, 300, 450):
        out.append(f"min{k//60}: leader_mid={np.nanmedian([lm(r['w'],k) for r in grp]):.3f} sum_ask={np.nanmedian([r['w']['ask'][:, k].sum() for r in grp]):.3f}")
    print(f"  {name:10s} n={len(grp):3d}  " + "  ".join(out))
# separability: leader mid at 5 min, wave vs not wave, as a rank statistic (probability a random wave window has a higher value than a random non-wave one)
for k in (180, 300):
    a = np.array([lm(r["w"], k) for r in wave]); b = np.array([lm(r["w"], k) for r in near + mixed])
    a, b = a[~np.isnan(a)], b[~np.isnan(b)]
    print(f"  P(wave leader mid at {k//60} min > non-wave) = {np.mean(a[:,None] > b[None,:]) + 0.5*np.mean(a[:,None]==b[None,:]):.3f} (0.5 = no separation), waves n={len(a)}, non-waves n={len(b)}")

print("\n(c) who leads: rank (1 = largest typical-minute move) of the winning coin among five, up/down mid curve; windows where all five measured")
for lab, key in (("PRIOR window (known before)", "pri"), ("same window (after the fact)", "cur")):
    print(" ", lab)
    for name, grp in (("up wave", [r for r in rows if r["nu"] == 5]), ("down wave", [r for r in rows if r["nu"] == 0]), ("mixed/near", near + mixed)):
        rk = []
        for r in grp:
            x = r[key]
            if np.isnan(x).any():
                continue
            wi = int(np.argmax(r["w"]["win"]))
            rk.append(1 + int((x > x[wi]).sum()))
        if rk:
            cnt = Counter(rk)
            print(f"    {name:10s} n={len(rk):3d} winner rank counts 1..5: {[cnt.get(i,0) for i in range(1,6)]}  mean rank {np.mean(rk):.2f} (3.0 = no relation)")
print("  coin labels of the winner by wave type (label only):")
for name, grp in (("up wave", [r for r in rows if r["nu"] == 5]), ("down wave", [r for r in rows if r["nu"] == 0]), ("mixed/near", near + mixed)):
    cc = Counter(COINS[int(np.argmax(r["w"]["win"]))] for r in grp)
    print(f"    {name:10s} n={len(grp):3d} {dict(cc)}")

print("\n(d) hedge check")
print("  yes contracts of the five leader markets pay exactly 1 in total in every window: windows with exactly one winner = all", len(W), "used.")
for name, grp in (("wave", wave), ("not wave", near + mixed)):
    for k in (300, 600):
        s = [r["w"]["ask"][:, k].sum() for r in grp if not np.isnan(r["w"]["ask"][:, k]).any()]
        f = [r["w"]["ask"][:, k].max() for r in grp if not np.isnan(r["w"]["ask"][:, k]).any()]
        print(f"  {name:8s} at {k//60} min: median cost to hold all five yes = {np.median(s):.3f} for a payout of 1 (a loss of {np.median(s)-1:.3f} before the fee); favourite ask median {np.median(f):.3f}")
