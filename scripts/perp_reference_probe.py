"""Does the reference price on Kalshi's own perps markets match what the 15-minute markets settle on? (2026-11-11, user pointed at Kalshi's perps websocket.) Read-only, public REST, no key.
Polls https://external-api.kalshi.com/trade-api/v2/margin/markets once a second from 25 s before the next quarter-hour to 25 s after, keeps each perp's reference price (divided by its contract size), then reads the 15-minute markets: the closed window's expiration_value and the new window's floor_strike, and prints them beside the perp reference at the boundary.
    python scripts/perp_reference_probe.py
"""

import json
import time
import urllib.request

PERPS = {"KXGOLDPERP": "KXGOLD15M", "KXSILVERPERP": "KXSILVER15M", "KXPLATINUMPERP": "KXPLATINUM15M", "KXPALLADIUMPERP": "KXPALLADIUM15M", "KXCOPPERPERP": "KXCOPPER15M", "KXWTIFUTURE": "KXWTI15M", "KXBRENTFUTURE": "KXBRENT15M", "KXHHGASFUTURE": "KXNATGAS15M", "KXBTCPERP": "KXBTC15M", "KXETHPERP": "KXETH15M", "KXUS500PERP": "KXINX15M"}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "probe"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


boundary = (int(time.time()) // 900 + 1) * 900
print("boundary", time.strftime("%H:%M:%S", time.localtime(boundary)), flush=True)
while time.time() < boundary - 25:
    time.sleep(1)
samples = {}
while time.time() < boundary + 25:
    t = time.time()
    try:
        for m in get("https://external-api.kalshi.com/trade-api/v2/margin/markets?limit=1000")["markets"]:
            if m["ticker"] in PERPS and m.get("reference_price"):
                rp = m["reference_price"]
                samples.setdefault(m["ticker"], []).append((rp["ts_ms"] / 1000.0, float(rp["price"]) / float(m["contract_size"]), (float(m["bid"]) + float(m["ask"])) / 2 / float(m["contract_size"]) if float(m.get("bid") or 0) > 0 else None))
    except Exception as exc:
        print("poll failed", exc)
    time.sleep(max(0.0, 1.0 - (time.time() - t)))
time.sleep(45)  # let the strike and the result post
for perp, ser in PERPS.items():
    s = sorted(set(samples.get(perp, [])))
    if not s:
        print(f"{ser}: no perp reference")
        continue
    at = min(s, key=lambda x: abs(x[0] - boundary))
    lo, hi = min(x[1] for x in s), max(x[1] for x in s)
    try:
        ms = get(f"https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker={ser}&limit=6")["markets"]
    except Exception as exc:
        print(ser, "markets failed", exc)
        continue
    closed = next((m for m in ms if m.get("close_time", "").startswith(time.strftime("%Y-%m-%dT%H:%M", time.gmtime(boundary)))), None)
    opened = next((m for m in ms if m.get("open_time", "").startswith(time.strftime("%Y-%m-%dT%H:%M", time.gmtime(boundary)))), None)
    ev = closed.get("expiration_value") if closed else None
    fs = opened.get("floor_strike") if opened else None
    def bp(x):
        try:
            return f"{(at[1] / float(x) - 1) * 1e4:+.1f} bp"
        except Exception:
            return "n/a"
    print(f"{ser:15s} perp reference at the boundary {at[1]:.5f} (ts {at[0] - boundary:+.1f}s; range in the 50 s {lo:.5f}-{hi:.5f}, {len(s)} distinct readings) | closed window's final {ev} ({bp(ev)}) | new window's target {fs} ({bp(fs)})")
