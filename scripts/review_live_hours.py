"""Review of the live lane's wins and losses over the last N hours (default 7). Every live bet bought in the window, its real result (own exit with the sale fee, or settlement; a market not yet booked is read from Kalshi), split by hour, exit type, buy price and time left at the buy.
Run on the host: HOURS=7 python scripts/review_live_hours.py"""
import collections
import datetime
import json
import os
import time
import urllib.request

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
HOURS = float(os.environ.get("HOURS", "7"))
since = time.time() - HOURS * 3600
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
rows = st.execute("SELECT ts, ticker, side, lane, contracts, stake_usd, exit_price, exit_ts, exit_reason, outcome, close_time FROM decisions WHERE action='bet' AND stake_usd>0 AND mode='trade' AND ts>? ORDER BY ts", (since,)).fetchall()
res_cache = {}


def result_of(ticker, outcome):
    if outcome in ("yes", "no"):
        return outcome
    if ticker not in res_cache:
        try:
            with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}", timeout=10) as f:
                r = json.load(f)["market"].get("result")
            res_cache[ticker] = r if r in ("yes", "no") else None
        except Exception:
            res_cache[ticker] = None
    return res_cache[ticker]


bets = []
for r in rows:
    n = float(r["contracts"]); cost = float(r["stake_usd"]); entry = cost / n
    close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp()
    if r["exit_price"] is not None:
        x = float(r["exit_price"]); v = n * (x - fee_per_contract(x)) - cost
        why = r["exit_reason"] or ""
        kind = "take" if "profit take" in why else "stop" if "max loss" in why else "other exit"
    else:
        res = result_of(r["ticker"], r["outcome"])
        if res is None:
            bets.append(dict(open=True, ts=float(r["ts"]), ticker=r["ticker"], cost=cost))
            continue
        v = (n if res == r["side"] else 0.0) - cost
        kind = "held, won" if v > 0 else "held, lost"
    bets.append(dict(open=False, ts=float(r["ts"]), ticker=r["ticker"][2:-3], side=r["side"], entry=entry, cost=cost, v=v, kind=kind, left=close - float(r["ts"])))
done = [b for b in bets if not b["open"]]
print(f"window: last {HOURS:g} h ({datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} to now ET); live bets bought: {len(bets)}; settled or sold: {len(done)}; still open or unread: {len(bets) - len(done)}")
tot = sum(b["v"] for b in done); staked = sum(b["cost"] for b in done)
wins = [b for b in done if b["v"] > 0]; losses = [b for b in done if b["v"] <= 0]
print(f"NET {tot:+.2f} on ${staked:.0f} staked ({tot / staked * 100:+.1f}%); wins {len(wins)} avg {sum(b['v'] for b in wins) / max(len(wins), 1):+.2f}; losses {len(losses)} avg {sum(b['v'] for b in losses) / max(len(losses), 1):+.2f}; win rate {len(wins) / max(len(done), 1) * 100:.0f}%")


def table(title, keyf, order=None):
    g = collections.OrderedDict()
    for b in done:
        k = keyf(b); e = g.setdefault(k, [0, 0.0, 0, 0.0]); e[0] += 1; e[1] += b["v"]; e[2] += (b["v"] > 0); e[3] += b["cost"]
    print(f"\n{title}")
    for k in (order or sorted(g)):
        if k in g:
            n, v, w, c = g[k]
            print(f"  {str(k):22s} n={n:3d} net {v:+8.2f}  won {w:2d}/{n:<3d}  per $1 staked {v / c:+.3f}")


table("by hour of the buy (ET)", lambda b: datetime.datetime.fromtimestamp(b["ts"], ET).strftime("%H:00"))
table("by how it ended", lambda b: b["kind"], ["take", "held, won", "held, lost", "stop", "other exit"])
table("by buy price", lambda b: "under 0.60" if b["entry"] < 0.60 else "0.60 to 0.80" if b["entry"] < 0.80 else "0.80 and over", ["under 0.60", "0.60 to 0.80", "0.80 and over"])
table("by seconds left at the buy", lambda b: "under 150 s" if b["left"] < 150 else "150 to 400 s" if b["left"] < 400 else "400 to 600 s" if b["left"] < 600 else "600 s and over", ["under 150 s", "150 to 400 s", "400 to 600 s", "600 s and over"])
table("by side", lambda b: b["side"])
table("by the lane's buy band 0.57 to 0.82 (user, 2026-11-15)", lambda b: "inside 0.57-0.82" if 0.57 <= b["entry"] <= 0.82 else "outside", ["inside 0.57-0.82", "outside"])
print("\nbiggest wins:")
for b in sorted(done, key=lambda b: -b["v"])[:5]:
    print(f"  {datetime.datetime.fromtimestamp(b['ts'], ET):%H:%M} {b['ticker']:22s} {b['side']} {b['entry']:.2f} {b['v']:+7.2f} {b['kind']}")
print("biggest losses:")
for b in sorted(done, key=lambda b: b["v"])[:6]:
    print(f"  {datetime.datetime.fromtimestamp(b['ts'], ET):%H:%M} {b['ticker']:22s} {b['side']} {b['entry']:.2f} {b['v']:+7.2f} {b['kind']}, {b['left']:.0f}s left at the buy")
print("DONE")
