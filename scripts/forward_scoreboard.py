"""Forward scoreboard: each change of 2026-11-15 scored only on bets placed AFTER it went live (user, 2026-11-15 night: "you tried this on past, not future states").

Every table written that day was computed on past bets, the same ones the settings were picked from. This reads only bets with a decision time after each change's go-live time (ET, 2026-11-15; edit CUTS when a change ships). For each change, what that change decides, on forward bets only:
  always = true (16:31): live bets since, net and per dollar.
  max_same_side = 2 (20:44): windows in which the live lane held three or more same-side bets (should be none) and the net of the bets since.
  long layer ladder (22:53): held-sale rows ("long layer") written since and what those bets ended with: held to a win, held to a loss.
  early take and sure hold (23:31): profit takes since by sale bid (any take at 0.95 and over after the first six minutes is a bug), early takes, and bets that reached 0.95 after six minutes with what they paid.
  curve edge floor -0.03 (23:35): live bets since (all at -0.03 or better), and, as the counterfactual, the paper variants' bets since at an edge under -0.03 (they still have the -0.05 floor): what the live floor kept out.
  entry-phase flags (not live: paper rest only): all lanes' bets since the paper gate shipped (23:31), flagged (z13 at or over 1.5, or z5 - z21 under -0.3) against not flagged.
Small n is said plainly. Rerun it and keep each dated output in studies/results; a rule's forward record is what decides whether it stays (CLAUDE.md rule 5 and 6).

Run on the host: python scripts/forward_scoreboard.py
"""
import bisect
import datetime
import json
import math
import os
import re
import urllib.request
from collections import defaultdict

from kalshi_moonshot import config, curverisk, db
from kalshi_moonshot.strategy import fee_per_contract
from kalshi_moonshot.watcher import perp_index

ET = datetime.timezone(datetime.timedelta(hours=-5))
D = (2026, 11, 15)
cut = lambda h, m: datetime.datetime(*D, h, m, tzinfo=ET).timestamp()
CUTS = {"always = true": cut(16, 31), "max_same_side = 2": cut(20, 44), "long layer ladder": cut(22, 53), "early take + sure hold": cut(23, 31), "curve edge floor -0.03": cut(23, 35), "entry-phase flags (paper)": cut(23, 31)}
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
res_cache = {}


def result(tk):
    if tk not in res_cache:
        try:
            res_cache[tk] = json.load(urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{tk}", timeout=10))["market"].get("result")
        except Exception:
            res_cache[tk] = None
    return res_cache[tk]


def bets_since(t0, lane_like):
    out = []
    for r in st.execute("SELECT lane, ts, ticker, side, contracts, stake_usd, exit_price, exit_ts, exit_reason, outcome, close_time, reason FROM decisions WHERE action='bet' AND stake_usd>0 AND lane LIKE ? AND ts>? ORDER BY ts", (lane_like, t0)).fetchall():
        n, cost = float(r["contracts"] or 0), float(r["stake_usd"])
        if n <= 0:
            continue
        res = r["outcome"] if r["outcome"] in ("yes", "no") else result(r["ticker"])
        if r["exit_price"] is not None:
            x = float(r["exit_price"]); v = n * (x - fee_per_contract(x)) - cost
        elif res in ("yes", "no"):
            v = (n if res == r["side"] else 0.0) - cost
        else:
            continue
        hold = (n if res == r["side"] else 0.0) - cost if res in ("yes", "no") else None
        out.append(dict(r=r, v=v, cost=cost, n=n, hold=hold))
    return out


def fmt(x):
    c = sum(b["cost"] for b in x)
    return f"n {len(x):3d} | won {sum(1 for b in x if b['v'] > 0) / max(len(x), 1):4.0%} | net ${sum(b['v'] for b in x):+8.2f} ({sum(b['v'] for b in x) / max(c, 1) * 100:+5.1f}% on ${c:6.0f})"


print("FORWARD SCOREBOARD, bets with a result placed after each change went live (a small n is not a verdict)")
live = {k: bets_since(t, "crypto_multiscale") for k, t in CUTS.items()}
live = {k: [b for b in v if b["r"]["lane"] == "crypto_multiscale"] for k, v in live.items()}
for k in ("always = true", "max_same_side = 2", "long layer ladder", "early take + sure hold", "curve edge floor -0.03"):
    print(f"  {k:28s} live: {fmt(live[k])}")
# same side clusters
by = defaultdict(list)
for b in live["max_same_side = 2"]:
    m = re.match(r"KX[A-Z]+15M-(26NOV\d{6})", b["r"]["ticker"])
    by[(m.group(1) if m else "?", b["r"]["side"])].append(b)
big = {k: len(v) for k, v in by.items() if len(v) >= 3}
print(f"\nmax_same_side = 2: windows with 3 or more same-side live bets since 20:44 (should be none): {big if big else 'none'}")
# long layer held rows
held_rows = []
try:
    for line in open(os.path.join(s.data_dir, "risk_shadow.jsonl")):
        if '"held"' in line and "long layer" in line:
            d = json.loads(line)
            if d["ts"] > CUTS["long layer ladder"]:
                held_rows.append(d)
except OSError:
    pass
tickers = sorted({d["ticker"] for d in held_rows})
done = [(tk, result(tk)) for tk in tickers]
print(f"long layer: {len(held_rows)} held-sale rows since 22:53 on {len(tickers)} bets; settled so far: {sum(1 for _, r in done if r)}")
byt = {b["r"]["ticker"] + b["r"]["side"]: b for b in live["long layer ladder"]}
for tk in tickers:
    b = next((x for x in live["long layer ladder"] if x["r"]["ticker"] == tk), None)
    if b:
        print(f"   held {tk[2:-3]:18s} {b['r']['side']:3s} ended ${b['v']:+7.2f} ({'sold' if b['r']['exit_price'] is not None else 'to settlement'})")
# takes
tk_rows = [b for b in live["early take + sure hold"] if "profit take" in str(b["r"]["exit_reason"] or "")]
print(f"\nprofit takes since 23:31: {len(tk_rows)}")
for b in tk_rows:
    r = b["r"]
    close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp()
    elapsed = 900 - (close - float(r["exit_ts"]))
    flag = "  <- at 0.95+ after six minutes: should have been held" if float(r["exit_price"]) >= 0.95 and elapsed > 360 else ("  (early take)" if "early take" in str(r["exit_reason"]) else "")
    print(f"   {r['ticker'][2:-3]:18s} sold at {float(r['exit_price']):.3f} {elapsed:4.0f}s into the market: ${b['v']:+7.2f}, holding {b['hold'] if b['hold'] is None else round(b['hold'], 2)}{flag}")
# edge floor counterfactual
cf = []
for b in bets_since(CUTS["curve edge floor -0.03"], "crypto_multiscale_%"):
    m = re.search(r"\[curve edge ([+-]?\d+\.\d+)\]", str(b["r"]["reason"] or ""))
    if m and float(m.group(1)) < -0.03:
        cf.append(b)
print(f"\ncurve edge floor, the counterfactual: paper variants' bets since 23:35 at an edge under -0.03 (the live floor kept these out): {fmt(cf)}")
# entry phase flags
allb = bets_since(CUTS["entry-phase flags (paper)"], "crypto_multiscale%")
flagged, kept = [], []
kpcache = {}
for b in allb:
    r = b["r"]; ser = re.match(r"(KX[A-Z]+15M)-", r["ticker"])
    if not ser:
        continue
    kp = perp_index(ser.group(1))
    g = tape.execute("SELECT DISTINCT ON (FLOOR(ts / 60)) ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY FLOOR(ts / 60), ts", (kp, float(r["ts"]) - 4000, float(r["ts"]) + 1)).fetchall() if kp else []
    ts, vs = [float(x[0]) for x in g], [float(x[1]) for x in g]
    z = curverisk.side_slopes(ts, vs, float(r["ts"]), r["side"]) if ts else None
    if z is None:
        continue
    (flagged if (z[13] >= 1.5 or z[5] - z[21] < -0.3) else kept).append(b)
print(f"entry-phase flags since 23:31, all crypto_multiscale lanes: flagged {fmt(flagged)}\n                                                         not flagged {fmt(kept)}")
print("DONE")
