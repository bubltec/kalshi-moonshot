"""Is one big taker print on the cheap side in the last seconds an indicator? The NATGAS finish: 155 contracts taken at 4c with 12 seconds left. Uses Kalshi's own trade records. Read-only.

For every finished 15-minute market of the listed series in the last DAYS days, the trades of its final 60 seconds are pulled (one request each, paced at 2 a second). A SIGNAL is the first single trade between 45 and 3 seconds before the close in which takers bought at least SIZE contracts of a side at a price of MAX_PX or less (a YES taker at yes_price <= MAX_PX, or a NO taker at no_price <= MAX_PX): somebody paying up for a cheap side in size. We then buy that side LAT seconds after the print, at the price of the first trade on that side at or after that moment (the price someone actually paid, within 8 s; if the price has already run past FILL_CAP (0.25) the ticket is "ran away", and if there is no trade, "no fill"). Held to settlement, taker fee included. A ticket wins if its side settles in its favor and pays $1.

    python - [DAYS=2] [LAT=2] < scripts/big_print_cheap_side_study.py          (inside the bot container; about 2,000 requests, so it takes about 20 minutes)
"""
import datetime
import os
import time

from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
c = KalshiClient(s.base_url, s.key_id, s.private_key_path, 2.0, s.page_size)

DAYS = float(os.environ.get("DAYS", "2"))
LAT = float(os.environ.get("LAT", "2"))
FILL_CAP = 0.25
SERIES = os.environ.get("SERIES", "KXBTC15M,KXETH15M,KXSOL15M,KXXRP15M,KXDOGE15M,KXGOLD15M,KXSILVER15M,KXWTI15M,KXNATGAS15M,KXCOPPER15M,KXPLATINUM15M,KXPALLADIUM15M").split(",")
GRID = [(size, px) for size in (100, 150, 300) for px in (0.06, 0.10)]
ts = lambda iso: datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()  # noqa: E731

hi = time.time() - 120
lo = hi - DAYS * 86400
markets = []
for series in SERIES:
    cursor = None
    while True:
        p = {"series_ticker": series, "status": "settled", "min_close_ts": int(lo), "max_close_ts": int(hi), "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        r = c._request("GET", "/markets", params=p)
        markets += [m for m in r.get("markets", []) if m.get("result") in ("yes", "no")]
        cursor = r.get("cursor")
        if not cursor or not r.get("markets"):
            break
print(f"{len(markets)} finished markets in {len(SERIES)} series over {DAYS:g} days", flush=True)

out = {g: {"signals": 0, "ran": 0, "nofill": 0, "tickets": []} for g in GRID}
done = 0
for m in markets:
    close = ts(m["close_time"])
    try:
        r = c._request("GET", "/markets/trades", params={"ticker": m["ticker"], "min_ts": int(close - 60), "max_ts": int(close), "limit": 1000})
    except Exception:
        continue
    trades = []
    for t in r.get("trades", []):
        trades.append((ts(t["created_time"]), float(t["yes_price_dollars"]), float(t["no_price_dollars"]), float(t["count_fp"]), t["taker_side"]))
    trades.sort()
    done += 1
    if done % 200 == 0:
        print(f"  {done}/{len(markets)}", flush=True)
    for size, px in GRID:
        cell = out[(size, px)]
        for t, yp, np_, n, side in trades:
            if not close - 45 <= t <= close - 3 or n < size:
                continue
            price = yp if side == "yes" else np_
            if price > px:
                continue
            cell["signals"] += 1
            nxt = [x for x in trades if x[0] >= t + LAT and x[0] <= t + LAT + 8 and x[4] == side]
            if not nxt:
                cell["nofill"] += 1
            else:
                paid = nxt[0][1] if side == "yes" else nxt[0][2]
                if paid > FILL_CAP:
                    cell["ran"] += 1
                else:
                    won = m["result"] == side
                    cell["tickets"].append((paid, won, (1.0 if won else 0.0) - paid - fee_per_contract(paid)))
            break

print(f"\n{done} markets with trades pulled; signal = one taker print of at least SIZE contracts at MAX_PX or less (a side), bought {LAT:g} s later at the next price paid, held to settlement")
print(f"{'print size':>10} {'price <=':>8} {'signals':>7} {'ran away':>8} {'no fill':>7} {'bought':>6} {'wins':>5} {'win%':>6} {'avg price':>9} {'P&L/ticket':>11} {'ROI':>7}")
for (size, px), cell in out.items():
    tk = cell["tickets"]
    if not tk:
        print(f"{size:>10} {px:>8.2f} {cell['signals']:>7} {cell['ran']:>8} {cell['nofill']:>7} {0:>6}")
        continue
    n = len(tk)
    wins = sum(1 for _p, w, _x in tk if w)
    price = sum(p for p, _w, _x in tk) / n * 100
    pnl = sum(x for _p, _w, x in tk) / n * 100
    print(f"{size:>10} {px:>8.2f} {cell['signals']:>7} {cell['ran']:>8} {cell['nofill']:>7} {n:>6} {wins:>5} {wins / n:>6.1%} {price:>8.1f}c {pnl:>+10.2f}c {pnl / price:>+7.0%}")
