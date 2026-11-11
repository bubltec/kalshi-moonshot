"""Would a 0.65 price floor and a minute-4 time floor have helped the live crypto lane? (2026-11-11, user: "would moving the price gate to 65 and the time gate to 4 on the low bound for crypto help?")
Question: on the live crypto_multiscale bets that settled, what do the bets kept by (ask >= 0.65 and bought from minute 4 of the 15) make, against the bets the rule would have refused, as they happened and held to settlement,
in two halves of the period, and how many of the total losses (no sale, side lost) fall on each side. One rule, the user's: nothing is swept. The bucket tables below are context for reading it, not choices.
The lane's band today is ask 0.55-0.75 and entry minutes 1.5-14. Dollars use each bet's stake_usd; as it happened = the lane's own sale (a resting-ask fill at its price; any other sale at the bid less the 0.02 slip and the
taker fee) or settlement; held = settlement; cost = ask plus the entry fee. Elapsed minutes = 15 - (close - buy)/60, close read from the ticker (ET).

    scripts/cloud.sh run scripts/crypto_gate_check.py
"""

import datetime as dt
import re

from kalshi_moonshot import config
from kalshi_moonshot.runner import EXIT_SLIP
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
ET = dt.timezone(dt.timedelta(hours=-5))
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731


def close_of(tk):
    m = re.match(r"[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)-", tk)
    return dt.datetime(2000 + int(m[1]), MON[m[2]], int(m[3]), int(m[4]), int(m[5]), tzinfo=ET).timestamp()


rows = st.db.execute(
    "SELECT ts, ticker, side, ask, stake_usd, outcome, exit_price, exit_ts, exit_reason FROM decisions WHERE action='bet' AND lane='crypto_multiscale' AND mode='trade' AND outcome IN ('yes','no') AND stake_usd>0 AND ask>0 ORDER BY ts"
).fetchall()
bets = []
for r in rows:
    try:
        elapsed = 15.0 - (close_of(r["ticker"]) - float(r["ts"])) / 60.0
    except Exception:
        continue
    ask, stake = float(r["ask"]), float(r["stake_usd"])
    cost = ask + fee(ask)
    won = 1.0 if r["outcome"] == (r["side"] or "yes") else 0.0
    held = stake * (won / cost - 1.0)
    if r["exit_price"] is not None:
        px = float(r["exit_price"])
        back = px if str(r["exit_reason"] or "").startswith("curve exit (resting ask)") else px - EXIT_SLIP - fee(px)
        real = stake * (back / cost - 1.0)
    else:
        real = held
    bets.append(dict(ts=float(r["ts"]), ask=ask, el=elapsed, stake=stake, real=real, held=held, total_loss=(r["exit_price"] is None and won == 0.0)))
print(f"{len(bets)} settled live crypto bets, {hm(bets[0]['ts'])} to {hm(bets[-1]['ts'])} ET")


def line(label, xs):
    if not xs:
        return f"  {label:34s} n=0"
    n = len(xs)
    st_ = sum(x["stake"] for x in xs)
    h = sorted(xs, key=lambda x: x["ts"])
    a, b = h[: n // 2], h[n // 2:]
    return (f"  {label:34s} n={n:3d} | as happened {sum(x['real'] for x in xs):+7.2f} ({sum(x['real'] for x in xs) / st_:+6.1%}) | held {sum(x['held'] for x in xs):+7.2f} | "
            f"total losses {sum(1 for x in xs if x['total_loss']):2d} (${sum(x['stake'] for x in xs if x['total_loss']):5.2f}) | halves {sum(x['real'] for x in a):+.2f} / {sum(x['real'] for x in b):+.2f}")


keep = [x for x in bets if x["ask"] >= 0.65 and x["el"] >= 4.0]
out = [x for x in bets if not (x["ask"] >= 0.65 and x["el"] >= 4.0)]
print("\nthe rule: ask >= 0.65 and bought from minute 4")
print(line("all bets (as it is today)", bets))
print(line("kept by the rule", keep))
print(line("refused by the rule", out))
print("\ncontext, one cut at a time")
print(line("ask >= 0.65", [x for x in bets if x["ask"] >= 0.65]))
print(line("ask < 0.65", [x for x in bets if x["ask"] < 0.65]))
print(line("minute >= 4", [x for x in bets if x["el"] >= 4.0]))
print(line("minute < 4", [x for x in bets if x["el"] < 4.0]))
print("\nprice buckets")
for lo, hi in ((0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 0.76)):
    print(line(f"ask {lo:.2f}-{hi:.2f}", [x for x in bets if lo <= x["ask"] < hi]))
print("elapsed buckets")
for lo, hi in ((0, 4), (4, 8), (8, 12), (12, 15)):
    print(line(f"minute {lo}-{hi}", [x for x in bets if lo <= x["el"] < hi]))
print("DONE")
