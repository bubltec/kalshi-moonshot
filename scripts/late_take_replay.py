"""Would the profit take's late cap have cashed a bet? One bet replayed on its own taped quotes, second by second, under the clock's bar alone (what ran live) and with the cap (2026-11-14).
The cap: once what is left to win is no more than the market's own swing over the time left (typical minute x sqrt(left / 60)), the bar is at most half the bet's room.
Run on the host: scripts/cloud.sh run scripts/late_take_replay.py TICKER=KXSOL15M-26NOV141530-30 SIDE=no ENTRY=0.78 BOUGHT=1794687609
It carries its own copy of the bar because the host runs the image's code, not this Mac's. Fee on both legs. One bet: an illustration, not a study."""
import math
import os
from datetime import UTC, datetime

from kalshi_moonshot import config, db, quoteshock
from kalshi_moonshot.curverisk import still_rising
from kalshi_moonshot.strategy import fee_per_contract

TICKER, SIDE = os.environ["TICKER"], os.environ.get("SIDE", "no")
ENTRY, BOUGHT, SHARE = float(os.environ["ENTRY"]), float(os.environ["BOUGHT"]), float(os.environ.get("SHARE", "0.08"))


def take_bar(gain, room, left, unit, cap):
    bar = min(max(1.0 - left / 900.0, 0.0), 1.0) * room
    if cap and unit and left > 0 and room - gain <= unit * math.sqrt(left / 60.0):
        bar = min(bar, room / 2.0)
    return bar


s = config.load()
tape = db.connect(s.tape_path, "tape", shared=True)
rows = [(float(ts), float(b), float(a)) for ts, b, a in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid>0 AND ask>0 ORDER BY ts", (TICKER,))]
close = datetime.strptime(TICKER.split("-")[1], "%y%b%d%H%M").replace(tzinfo=UTC).timestamp() + 5 * 3600  # the ticker's time is New York (EST)
mids = [(ts, (b + a) / 2.0) for ts, b, a in rows]
basis = ENTRY + fee_per_contract(ENTRY)
room = 1.0 - basis
print(f"{TICKER} {SIDE} bought at {ENTRY} ({basis:.3f} with the fee, room {room:.3f}); {len(rows)} quotes; close {close:.0f}")
for cap in (False, True):
    hist, sold, i = [], None, 0
    t = math.ceil(BOUGHT)
    while t < close - 8:
        while i < len(rows) and rows[i][0] <= t:
            i += 1
        if i:
            _, b, a = rows[i - 1]
            bid = b if SIDE == "yes" else 1.0 - a
            left = close - t
            hist.append((float(t), bid))
            gain = bid - fee_per_contract(bid) - basis
            unit = quoteshock.typical_minute([m for m in mids if m[0] <= t], float(t))
            look = SHARE * left
            if gain > fee_per_contract(bid) and gain >= take_bar(gain, room, left, unit, cap) and not still_rising(hist, float(t), look, unit):
                sold = (t, bid, gain, left, unit)
                break
        t += 1
    label = "with the late cap" if cap else "the clock's bar alone (live)"
    if sold:
        t, bid, gain, left, unit = sold
        print(f"  {label}: sells at {bid:.2f} with {left:.0f}s left, {gain:+.3f} a contract (typical minute {unit or 0:.3f})")
    else:
        print(f"  {label}: no profit sale")
print("DONE")
