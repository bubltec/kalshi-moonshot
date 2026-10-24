"""Hourly crypto ladders against 15-minute crypto windows: favorites held to settlement, and the spread you pay.
Read-only: it downloads settled markets and their 1-minute candles from Kalshi and reads the tape. Places nothing.
For each settled hourly event (KXBTCD, KXETHD, KXETH) it takes the 'above K' markets (KXBTCD, KXETHD and KXETH; KXBTC itself is a set of narrow brackets, never a favorite) whose strike was nearest the spot price 30 minutes before close (chosen
from the tape's index ticks, so the choice does not look at how the hour ended), fetches their candles, and at 45/30/15/10/5 minutes before close asks:
if a side was priced inside each band (45-60c, 40-65c, 63-89c), what did holding it to settlement earn after fees, and how wide was the spread. The 15-minute side uses the same
instruments' windows at the same minutes before close (10 and 5 minutes left = minutes 5 and 10 of the window).
    docker compose exec -T bot python - < scripts/hourly_study.py
"""
import bisect
import datetime as dt
import json
import math
import os
import time
from collections import defaultdict

from kalshi_moonshot import config
from kalshi_moonshot.__main__ import _client
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot.tape import Tape

s = config.reload()
c = _client(s)
st = Store(s.db_path, s.exclude_versions)
tape = Tape(s.tape_path).db
BANDS = ((0.45, 0.60), (0.40, 0.65), (0.63, 0.89))  # the near-the-money bands for an hourly ladder, and the favorites band used on the 15-minute markets
LEFT = (45, 30, 15, 10, 5)  # minutes before close
PER_EVENT = 14
EVENTS = 60


def ts_of(iso):
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def settled(series):
    out, cursor = [], ""
    for _ in range(40):
        p = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        body = c._request("GET", "/markets", params=p)
        out += body.get("markets", [])
        cursor = body.get("cursor") or ""
        events = {m["event_ticker"] for m in out}
        if not cursor or len(events) >= EVENTS + 2:
            break
    return out


def spot_at(index_id, ts, cache={}):
    if index_id not in cache:
        rows = tape.execute("select ts, value from index_ticks where index_id=? order by ts", (index_id,)).fetchall()
        cache[index_id] = ([r[0] for r in rows], [r[1] for r in rows])
    xs, vs = cache[index_id]
    i = bisect.bisect_right(xs, ts) - 1
    return vs[i] if i >= 0 and ts - xs[i] < 120 else None


OBS = "/tmp/hourly_obs.json"
obs = []  # (series, event, left, side_ask, side_bid, won)
if os.path.exists(OBS):
    obs = json.load(open(OBS))
    print("loaded", len(obs), "saved observations", flush=True)
else:
    for series, base in (("KXBTCD", "KXBTC15M"), ("KXETHD", "KXETH15M"), ("KXETH", "KXETH15M")):
        idx = index_for_series(base)
        ms = [m for m in settled(series) if m.get("strike_type") == "greater" and m.get("result") in ("yes", "no")]
        by = defaultdict(list)
        for m in ms:
            by[m["event_ticker"]].append(m)
        events = sorted(by, key=lambda e: by[e][0]["close_time"], reverse=True)[:EVENTS]
        print(f"{series}: {len(events)} events with 'above K' markets, index {idx}", flush=True)
        done = 0
        for ev in events:
            group = by[ev]
            close = ts_of(group[0]["close_time"])
            spot = spot_at(idx, close - 30 * 60)
            if spot is None:
                continue
            for m in sorted(group, key=lambda m: abs(float(m["floor_strike"]) - spot))[:PER_EVENT]:
                try:
                    cs = c._request("GET", f"/series/{series}/markets/{m['ticker']}/candlesticks",
                                    params={"start_ts": int(close - 3900), "end_ts": int(close), "period_interval": 1}).get("candlesticks", [])
                except Exception as exc:
                    print("  candle fetch failed", m["ticker"], str(exc)[:60], flush=True)
                    continue
                cs.sort(key=lambda x: int(x["end_period_ts"]))
                ends = [int(x["end_period_ts"]) for x in cs]  # a candle exists only for minutes the quote changed in: carry the last one forward
                for left in LEFT:
                    i = bisect.bisect_right(ends, int(close - left * 60)) - 1
                    if i < 0:
                        continue
                    try:
                        bid, ask = float(cs[i]["yes_bid"]["close_dollars"]), float(cs[i]["yes_ask"]["close_dollars"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if ask > 0 and ask >= bid:
                        for side, a, b in (("yes", ask, bid), ("no", 1 - bid, 1 - ask)):
                            obs.append((series, ev, left, a, b, 1.0 if m["result"] == side else 0.0))
            done += 1
            if done % 10 == 0:
                print(f"  {series} {done}/{len(events)} events", flush=True)
    json.dump(obs, open(OBS, "w"))

# 15-minute windows, BTC and ETH, from the stored candles (minute i ends at open + 60 i, so 'left' = 15 - i)
for ser in ("KXBTC15M", "KXETH15M"):
    for w in st.db.execute("select ticker, close_ts, result from windows where series=? and result in ('yes','no')", (ser,)).fetchall():
        for r in st.db.execute("select end_ts, bid_c, ask_c from candles where ticker=?", (w["ticker"],)).fetchall():
            left = round((float(w["close_ts"]) - float(r["end_ts"])) / 60)
            if left in LEFT and r["bid_c"] is not None and r["ask_c"] is not None:
                bid, ask = float(r["bid_c"]), float(r["ask_c"])
                for side, a, b in (("yes", ask, bid), ("no", 1 - bid, 1 - ask)):
                    obs.append(("15M " + ser[2:5], w["ticker"], left, a, b, 1.0 if w["result"] == side else 0.0))


def line(rows):
    if len(rows) < 8:
        return f"n={len(rows):>5}  too few"
    byev = defaultdict(list)
    for ev, a, b, won in rows:
        byev[ev].append(won / (a + fee(a)) - 1)
    means = [sum(v) / len(v) for v in byev.values()]
    m = sum(w / (a + fee(a)) - 1 for _, a, _, w in rows) / len(rows)
    em = sum(means) / len(means)
    se = math.sqrt(sum((x - em) ** 2 for x in means) / (len(means) - 1) / len(means)) if len(means) > 1 else 0
    spread = sorted(a - b for _, a, b, _ in rows)[len(rows) // 2]
    return f"n={len(rows):>5} in {len(byev):>3} events  ROI {m:+6.1%} (event-level ±{se:.1%})  median spread {spread:.3f}"


groups = ["KXBTCD", "KXETHD", "KXETH", "15M BTC", "15M ETH"]
print("\nHolding a side to settlement after fees. 'hourly' = near-the-money 'above K' ladder markets; '15M' = the 15-minute window's own market.")
for lo, hi in BANDS:
    print(f"\n=========== side priced {lo:.0%}-{hi:.0%} ===========")
    for left in LEFT:
        print(f"--- {left} minutes before close")
        for g in groups:
            if g.startswith("15M") and left > 14:
                continue
            rows = [(ev, a, b, w) for (sr, ev, lf, a, b, w) in obs if sr == g and lf == left and lo <= a <= hi]
            print(f"  {g:8} {line(rows)}")
