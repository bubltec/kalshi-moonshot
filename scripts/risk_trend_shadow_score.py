"""The risk trend at the buy, scored in shadow against the old price trend on the same decisions (2026-11-17, user: "tell me how the new risk trend gate decide buys is working before we go live with it").
One question: of the moments the live crypto lane looked at a side, did the ones the risk trend would have bought do better than the ones it would not, and how does that compare with the old 15 s price trend's own split?
Same decisions: every crypto_multiscale decision (buy or pass) whose note carries the shadow tag "[risk trend ... | price trend ...]" and whose market has settled. Each is scored as a buy of that side at the ask it saw, held to settlement, fee in: (1 if the side won else 0) - ask - fee, per contract, and per dollar staked. These are moments the lane LOOKED at, most of them turned down by another gate, so this says how each reading sorts candidates, not what the lane earned.
Readings inside one market are not independent: n is decisions, and the markets are counted beside it. The first decision per market and side is also shown alone.
By curve shape: the look back itself (short = late in the market, long = early) and the size of the reading, which are the features this reading has. Settings: none chosen after seeing this output; the buckets are fixed here.
Run on the host: scripts/cloud.sh run scripts/risk_trend_shadow_score.py"""
import datetime
import re

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
rows = st.execute("SELECT ts, ticker, side, action, ask, outcome, reason FROM decisions WHERE lane='crypto_multiscale' AND reason LIKE '%[risk trend%' ORDER BY ts").fetchall()
TAG = re.compile(r"\[risk trend ([+-][0-9.]+) over (\d+)s: ([a-z, ]+?)(?: \| price trend (?:([+-][0-9.]+) over \d+s|unreadable))?\]")
UNREAD = re.compile(r"\[risk trend unreadable(?: \| price trend (?:([+-][0-9.]+) over \d+s|unreadable))?\]")

got, unread, unsettled = [], 0, 0
for r in rows:
    reason = r["reason"] or ""
    if r["outcome"] not in ("yes", "no") or r["ask"] is None:
        unsettled += 1
        continue
    m = TAG.search(reason)
    if not m:
        unread += 1 if UNREAD.search(reason) else 0
        continue
    ask = float(r["ask"])
    net = (1.0 if r["outcome"] == r["side"] else 0.0) - ask - fee_per_contract(ask)
    got.append({"ts": float(r["ts"]), "tk": r["ticker"], "side": r["side"], "z": float(m.group(1)), "back": float(m.group(2)), "new": m.group(3).strip(), "old": None if m.group(4) is None else float(m.group(4)), "ask": ask, "net": net, "won": r["outcome"] == r["side"], "bet": r["action"] == "bet"})

first = min((g["ts"] for g in got), default=0.0)
last = max((g["ts"] for g in got), default=0.0)
fmt = lambda t: datetime.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")
print(f"window: {fmt(first)} to {fmt(last)} ET; {len(rows)} tagged decisions, {len(got)} settled with a reading, {unread} settled but unreadable, {unsettled} not settled yet")
print(f"markets: {len({g['tk'] for g in got})}; real buys among them: {sum(g['bet'] for g in got)}")


def line(label, xs):
    if not xs:
        print(f"  {label:<46} n   0")
        return
    cost = sum(x["ask"] + fee_per_contract(x["ask"]) for x in xs)
    net = sum(x["net"] for x in xs)
    print(f"  {label:<46} n {len(xs):>3} | markets {len({x['tk'] for x in xs}):>3} | won {sum(x['won'] for x in xs):>3} | avg ask {sum(x['ask'] for x in xs) / len(xs):.2f} | net per contract {net / len(xs):+.3f} | net per $1 {net / cost:+.3f}")


def block(title, xs):
    print(f"\n{title}")
    line("all", xs)
    print(" the risk trend (new, shadow):")
    line("would buy", [x for x in xs if x["new"] == "would buy"])
    line("would not buy", [x for x in xs if x["new"] == "would not buy"])
    line("anomaly, would sit out", [x for x in xs if x["new"].startswith("anomaly")])
    print(" the price trend over 15 s (old, deciding now):")
    line("passes (over 0)", [x for x in xs if x["old"] is not None and x["old"] > 0])
    line("refuses (0 or under)", [x for x in xs if x["old"] is not None and x["old"] <= 0])
    line("unreadable", [x for x in xs if x["old"] is None])
    print(" the two together:")
    for new in ("would buy", "would not buy"):
        for name, test in (("old passes", lambda o: o is not None and o > 0), ("old refuses", lambda o: o is not None and o <= 0)):
            line(f"new {new}, {name}", [x for x in xs if x["new"] == new and test(x["old"])])


block("A. every settled decision with a reading", got)
seen, firsts = set(), []
for g in got:
    if (g["tk"], g["side"]) not in seen:
        seen.add((g["tk"], g["side"]))
        firsts.append(g)
block("B. the first decision per market and side only (one row a market side)", firsts)

print("\nC. by the reading's own shape (every settled decision)")
print(" by the look back (a quarter of the time left):")
for lo, hi in ((0, 45), (45, 90), (90, 135), (135, 999)):
    xs = [x for x in got if lo <= x["back"] < hi]
    line(f"look back {lo} to {hi if hi < 999 else 'up'} s: would buy", [x for x in xs if x["new"] == "would buy"])
    line(f"look back {lo} to {hi if hi < 999 else 'up'} s: would not buy", [x for x in xs if x["new"] == "would not buy"])
print(" by the size of the reading (typical moves):")
for lo, hi, name in ((-99, -2, "under -2"), (-2, -1, "-2 to -1"), (-1, 0, "-1 to 0"), (0, 1, "0 to +1"), (1, 2, "+1 to +2"), (2, 99, "over +2")):
    line(f"risk trend {name}", [x for x in got if lo <= x["z"] < hi])
print("DONE")
