"""Late-window tickets: does 'the price is close to the strike' beat what the Kalshi quote already says?

For every stored settled 15-minute window, at 3, 2 and 1 minutes left, take each side priced 1-25 cents and
measure how far the underlying feed is from the strike in units of its own expected move over the time left
(`need`, in sigmas; 0 = at the strike, -2 = two sigmas away). Then compare, per bucket, how often the side won with
what it cost. Uses only what was knowable at that minute: the feed's 1-minute closes, a basis learned from earlier
boundaries, and the prior three hours of volatility. Places no orders.

Run from the project root with the history database copied somewhere:
    python scripts/late_ticket_study.py [path/to/moonshot.db] [low high [dump.json]]     (price band in dollars, default 0.01 0.25; dump writes every entry for modelling)
"""
import json, math, sqlite3, statistics, sys, time, urllib.request
from collections import defaultdict
from statistics import NormalDist

from kalshi_moonshot import commodities as cm
from kalshi_moonshot.strategy import fee_per_contract as fee

N = NormalDist()
VOLM = 1.25
FEEDS = {"KXGOLD15M": "hl:GOLD", "KXSILVER15M": "hl:SILVER", "KXWTI15M": "hl:CL", "KXCOPPER15M": "hl:COPPER",
         "KXNATGAS15M": "hl:NATGAS", "KXPLATINUM15M": "hl:PLATINUM", "KXPALLADIUM15M": "hl:PALLADIUM",
         "KXBTC15M": "kr:BTC/USD", "KXETH15M": "kr:ETH/USD", "KXSOL15M": "kr:SOL/USD", "KXXRP15M": "kr:XRP/USD",
         "KXDOGE15M": "kr:DOGE/USD"}


def hl_minutes(coin):
    now = int(time.time() * 1000)
    body = {"type": "candleSnapshot", "req": {"coin": "xyz:" + coin, "interval": "1m", "startTime": now - 4900 * 60_000, "endTime": now}}
    req = urllib.request.Request("https://api.hyperliquid.xyz/info", json.dumps(body).encode(), {"Content-Type": "application/json"})
    return {int(r["t"] // 1000): float(r["c"]) for r in json.load(urllib.request.urlopen(req, timeout=20))}


def kr_minutes(sym):
    pair = sym.replace("BTC/", "XBT/").replace("/", "")
    d = json.load(urllib.request.urlopen(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1", timeout=20))
    rows = next(v for k, v in d["result"].items() if k != "last")
    return {int(float(r[0])): float(r[4]) for r in rows}


LO, HI = (float(sys.argv[2]), float(sys.argv[3])) if len(sys.argv) > 3 else (0.01, 0.25)
db = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else "moonshot.db")
db.row_factory = sqlite3.Row
rows = []
for series, feed in FEEDS.items():
    mins = hl_minutes(feed[3:]) if feed.startswith("hl:") else kr_minutes(feed[3:])
    wins = db.execute("select ticker, close_ts, strike, result from windows where series=? and result in ('yes','no') and strike is not null",
                      (series,)).fetchall()
    refs = {}
    for w in wins:
        refs[w["close_ts"] - 900] = w["strike"]           # the strike is the reference at the window's open
    for w in wins:
        close, open_ = w["close_ts"], w["close_ts"] - 900
        cs = {r["end_ts"]: r for r in db.execute("select end_ts, bid_c, ask_c from candles where ticker=?", (w["ticker"],))}
        for left in (3, 2, 1):
            at = close - left * 60
            if at - 3 * 3600 < min(mins, default=at):
                continue
            wb = cm.window_basis({t: v for t, v in refs.items() if t <= open_}, mins, float(at))
            px = mins.get(at - 60)
            c = cs.get(at)
            if wb is None or px is None or c is None or c["bid_c"] is None or c["ask_c"] is None:
                continue
            cl = [mins[t] for t in range(at - 3 * 3600, at, 60) if t in mins]
            if len(cl) < 60:
                continue
            rets = [math.log(b / a) for a, b in zip(cl, cl[1:]) if a > 0 and b > 0]
            s1 = math.sqrt(sum(r * r for r in rets) / len(rets))
            sigma = max(s1 * math.sqrt(left) * VOLM, 1e-6)
            ln_ratio = math.log(px * math.exp(wb.basis) / w["strike"])
            prev = mins.get(at - 180)
            mom = math.log(px / prev) / (s1 * math.sqrt(2)) if prev else 0.0   # last 2 minutes, in 2-minute sigmas
            trend = {k: (math.log(px / mins[at - 60 - 60 * k]) if (at - 60 - 60 * k) in mins else None) for k in (5, 15, 60)}
            for side, price in (("yes", c["ask_c"]), ("no", 1 - c["bid_c"])):
                if not LO <= price <= HI:
                    continue
                need = (ln_ratio if side == "yes" else -ln_ratio) / sigma    # how far from winning, in sigmas
                toward = mom if side == "yes" else -mom                      # recent move toward winning
                sgn = 1 if side == "yes" else -1
                move = {k: (None if (at - 60 - 60 * k) not in mins else sgn * math.log(px / mins[at - 60 - 60 * k]))
                        for k in (5, 15, 60)}                                # change over the past 5/15/60 minutes, toward winning
                rows.append(dict(series=series, t=close, left=left, side=side, price=price, need=need, toward=toward,
                                 s1=s1, spread=c["ask_c"] - c["bid_c"], won=w["result"] == side,
                                 model=N.cdf(need), dist=-sgn * ln_ratio, tr5=move[5], tr15=move[15], tr60=move[60]))
    print(series, len(wins), "windows", flush=True)

rows.sort(key=lambda r: r["t"])
if len(sys.argv) > 4:
    json.dump(rows, open(sys.argv[4], "w"))
print(f"\n{len(rows)} entries in {len({(r['series'], r['t']) for r in rows})} windows\n")


def roi(g):
    cost = sum(r["price"] + fee(r["price"]) for r in g)
    return sum(r["won"] for r in g) / cost - 1 if cost else float("nan")


def show(label, g, base=None):
    if len(g) < 15:
        return
    wr = sum(r["won"] for r in g) / len(g)
    ap = statistics.mean(r["price"] for r in g)
    print(f"{label:46} n={len(g):5} paid {ap:.3f} won {wr:5.1%} model {statistics.mean(r['model'] for r in g):5.1%} ROI {roi(g):+6.1%}")


show(f"everything {LO:.2f}-{HI:.2f}", rows)
print("\nBy how far the feed is from the strike (sigmas to go; 0 = at the strike):")
for lo, hi in ((-9, -2), (-2, -1.5), (-1.5, -1), (-1, -0.5), (-0.5, 0), (0, 9)):
    show(f"  need {lo:+.1f}..{hi:+.1f}", [r for r in rows if lo <= r["need"] < hi])
print("\nSame, only when the feed has been moving toward the strike (last 2 minutes):")
for lo, hi in ((-2, -1), (-1, -0.5), (-0.5, 0)):
    show(f"  need {lo:+.1f}..{hi:+.1f}, moving toward", [r for r in rows if lo <= r["need"] < hi and r["toward"] > 0.5])
    show(f"  need {lo:+.1f}..{hi:+.1f}, moving away", [r for r in rows if lo <= r["need"] < hi and r["toward"] < -0.5])
print("\nBy minutes left, for need within 1 sigma:")
for left in (3, 2, 1):
    show(f"  {left} min left", [r for r in rows if r["left"] == left and r["need"] >= -1])
print("\nMarket price against the model's chance (model - price), whole sample:")
for lo, hi in ((-1, -0.05), (-0.05, 0), (0, 0.03), (0.03, 0.08), (0.08, 1)):
    show(f"  model - price in {lo:+.2f}..{hi:+.2f}", [r for r in rows if lo <= r["model"] - r["price"] < hi])
print("\nBy series family:")
for name, keys in (("crypto", ("BTC", "ETH", "SOL", "XRP", "DOGE")), ("metals", ("GOLD", "SILVER", "COPPER", "PLATINUM", "PALLADIUM")), ("energy", ("WTI", "NATGAS"))):
    show(f"  {name}, need >= -1", [r for r in rows if any(k in r["series"] for k in keys) and r["need"] >= -1])
cut = rows[int(len(rows) * 0.6)]["t"]
print("\nEarlier 60% vs later 40% of the days, need >= -1:")
show("  earlier", [r for r in rows if r["t"] < cut and r["need"] >= -1])
show("  later", [r for r in rows if r["t"] >= cut and r["need"] >= -1])
