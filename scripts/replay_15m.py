"""Replay settled 15-minute windows: would the data-driven model have beaten the market at the 10-minute mark?

Run it from the project root:  python scripts/replay_15m.py  [delete the cache file to refetch]
It uses only public data (Kalshi settled markets and minute candles, Hyperliquid and Kraken candles), places
no orders, and replays each window with what the bot would have known then: the feed price at the decision
minute, the offset learned from earlier windows only, and the previous three hours of volatility.
"""
import json, math, os, statistics, sys, time, urllib.request
from datetime import datetime
from kalshi_moonshot import commodities as cm
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.strategy import fee_per_contract as fee
ELAPSED = 10.0; VOLM = 1.25; FLOOR = 0.005
c = KalshiClient("https://external-api.kalshi.com/trade-api/v2", max_rps=8)
CACHE = os.path.join(os.environ.get("DATA_DIR", "."), "replay_15m.cache.json")
FEEDS = {"KXGOLD15M":"hl:xyz:GOLD","KXSILVER15M":"hl:xyz:SILVER","KXWTI15M":"hl:xyz:CL","KXCOPPER15M":"hl:xyz:COPPER","KXNATGAS15M":"hl:xyz:NATGAS",
         "KXPLATINUM15M":"hl:xyz:PLATINUM","KXPALLADIUM15M":"hl:xyz:PALLADIUM","KXBTC15M":"kraken:BTC/USD","KXETH15M":"kraken:ETH/USD",
         "KXSOL15M":"kraken:SOL/USD","KXXRP15M":"kraken:XRP/USD","KXDOGE15M":"kraken:DOGE/USD"}
def hl_minutes(coin):
    now = int(time.time()*1000); out = {}
    req = urllib.request.Request("https://api.hyperliquid.xyz/info", json.dumps({"type":"candleSnapshot","req":{"coin":coin,"interval":"1m","startTime":now-4900*60_000,"endTime":now}}).encode(), {"Content-Type":"application/json"})
    for r in json.load(urllib.request.urlopen(req, timeout=20)): out[int(r["t"]//1000)] = float(r["c"])
    return out
def kr_minutes(sym):
    pair = sym.replace("BTC/","XBT/").replace("/","")
    d = json.load(urllib.request.urlopen(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1", timeout=20))
    rows = next(v for k,v in d["result"].items() if k!="last")
    return {int(float(r[0])): float(r[4]) for r in rows}
def cd(k, f):
    v = (k.get(f) or {}).get("close_dollars"); return float(v) if v not in (None,"") else None
if os.path.exists(CACHE): data = json.load(open(CACHE))
else:
    data = {}
    for ser, sym in FEEDS.items():
        ms = c.settled_markets(ser, limit=80)
        mins = hl_minutes(sym[3:]) if sym.startswith("hl:") else kr_minutes(sym[7:])
        lo = min(mins) + 3*3600
        wins = []
        for m in ms:
            if m.get("result") not in ("yes","no") or m.get("floor_strike") is None: continue
            close = int(datetime.fromisoformat(m["close_time"]).timestamp()); at = close - 900 + int(ELAPSED*60)
            if at < lo: continue
            try: cs = c._request("GET", f"/series/{ser}/markets/{m['ticker']}/candlesticks", params={"start_ts": at-180, "end_ts": at+60, "period_interval": 1}).get("candlesticks", [])
            except Exception: continue
            best = None
            for k in cs:
                if k["end_period_ts"] <= at: best = k
            if not best: continue
            ya, yb = cd(best,"yes_ask"), cd(best,"yes_bid")
            if not ya or not yb or ya >= 1 or yb <= 0: continue
            wins.append({"close":close,"strike":float(m["floor_strike"]),"exp":m.get("expiration_value"),"ya":ya,"yb":yb,"res":m["result"]})
        refs = cm.boundary_refs(ms)
        data[ser] = {"mins":{str(k):v for k,v in mins.items()}, "wins":wins, "refs":{str(k):v for k,v in refs.items()}}
        print(ser, len(wins), "windows", flush=True)
    json.dump(data, open(CACHE,"w"))
rows = []
for ser, d in data.items():
    mins = {int(k):v for k,v in d["mins"].items()}; refs = {int(k):v for k,v in d["refs"].items()}
    for w in d["wins"]:
        close = w["close"]; open_ = close-900; at = open_+int(ELAPSED*60)
        prior = {t:r for t,r in refs.items() if t <= open_}
        wb = cm.window_basis(prior, mins, float(at))
        px = mins.get(at-60)                       # candle closing at the 10-minute mark
        if wb is None or px is None: continue
        # volatility: 1-minute returns over the 3 hours before the decision
        cl = [mins[t] for t in range(at-3*3600, at, 60) if t in mins]
        if len(cl) < 60: continue
        rets = [math.log(b/a) for a,b in zip(cl, cl[1:]) if a>0 and b>0]
        s1 = math.sqrt(sum(r*r for r in rets)/len(rets))
        rem = (close-at)/60.0
        sigma = s1*math.sqrt(rem)*VOLM
        total = math.sqrt(sigma**2 + max(wb.sd,1.5e-4)**2)
        center = px*math.exp(wb.basis)
        p = min(max(cm.prob_above(center, w["strike"], total), FLOOR), 1-FLOOR)
        rows.append((ser, w["ya"], w["yb"], w["res"], p))
print(f"{len(rows)} replayed windows")
def sides(r):
    ser, ya, yb, res, p = r
    return [("yes", ya, p, 1.0 if res=="yes" else 0.0), ("no", 1-yb, 1-p, 1.0 if res=="no" else 0.0)]
def report(name, sel):
    if not sel: print(f"{name:52} none"); return
    cost = sum(pr+fee(pr) for pr,_ in sel); win = sum(w for _,w in sel)
    print(f"{name:52} n={len(sel):4} avg price {statistics.mean(pr for pr,_ in sel):.3f} won {statistics.mean(w for _,w in sel):.3f} ROI {(win-cost)/cost:+.1%}")
# Brier: model vs market mid
bm = statistics.mean((r[4]-(1.0 if r[3]=="yes" else 0.0))**2 for r in rows)
bk = statistics.mean((((r[1]+r[2])/2)-(1.0 if r[3]=="yes" else 0.0))**2 for r in rows)
print(f"Brier  model {bm:.4f}   market mid {bk:.4f}   (lower is better)")
for lo_, hi_ in ((0.70,0.90),(0.30,0.70),(0.0,0.30)):
    pass
for label, band, mn in (("model says edge >= 1.5c, entry 70-90c", (0.70,0.90), 0.015),
                        ("model says edge >= 5c,   entry 70-90c", (0.70,0.90), 0.05),
                        ("model says edge >= 5c,   entry <= 30c", (0.0,0.30), 0.05),
                        ("model says edge >= 10c,  entry <= 30c", (0.0,0.30), 0.10),
                        ("model says edge >= 5c,   any entry <= 90c", (0.0,0.90), 0.05)):
    sel = [(pr, w) for r in rows for (_, pr, ps, w) in sides(r) if band[0] <= pr <= band[1] and ps - pr - fee(pr) >= mn]
    report(label, sel)
print("--- calibration of the model's own chance of YES")
for lo_, hi_ in ((0,.1),(.1,.3),(.3,.5),(.5,.7),(.7,.9),(.9,1.01)):
    g = [r for r in rows if lo_ <= r[4] < hi_]
    if g: print(f"  model {lo_:.1f}-{min(hi_,1):.1f}  n={len(g):4}  model mean {statistics.mean(r[4] for r in g):.3f}  YES won {statistics.mean(1.0 if r[3]=='yes' else 0.0 for r in g):.3f}  market mid {statistics.mean((r[1]+r[2])/2 for r in g):.3f}")
