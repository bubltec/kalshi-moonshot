"""Would the squeeze (paper, lane field squeeze_seconds) have sold a losing bet? One bet replayed on its own taped quotes, second by second (2026-11-14).
The squeeze: a losing bet at, past or within the market's noise over 5 s of the max-loss stop or the 0.50 line is sold at once when its bid is sliding over the look back (8% of the time left): fallen by more than its noise, not bounced, and already down half the look back ago.
Run on the host: scripts/cloud.sh run scripts/squeeze_replay.py TICKER=... SIDE=no ENTRY=0.78 BOUGHT=<epoch>
HOLD=1 puts the swing hold in front of it, as the second version does (a loss under 1.5 x typical minute x sqrt(left / 60) is held while the bid net of its fee is at least 0.10): then time left decides whether the squeeze acts at all. HOLD=0 is the first version, which went ahead of the hold.
The hour view (curve_risk_confirm), which can still hold a squeezed sale, is NOT replayed here. Fee on both legs. One bet: an illustration, not a study."""
import math
import os
from datetime import UTC, datetime

from kalshi_moonshot import config, curverisk, db, quoteshock
from kalshi_moonshot.strategy import fee_per_contract

TICKER, SIDE = os.environ["TICKER"], os.environ.get("SIDE", "no")
ENTRY, BOUGHT = float(os.environ["ENTRY"]), float(os.environ["BOUGHT"])
SHARE, SECONDS, LINE, MAX_LOSS = 0.08, 5.0, 0.5, 0.5
HOLD, SWING, FLOOR = os.environ.get("HOLD", "1") == "1", 1.5, 0.10

s = config.load()
tape = db.connect(s.tape_path, "tape", shared=True)
rows = [(float(ts), float(b), float(a)) for ts, b, a in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid>0 AND ask>0 ORDER BY ts", (TICKER,))]
close = datetime.strptime(TICKER.split("-")[1], "%y%b%d%H%M").replace(tzinfo=UTC).timestamp() + 5 * 3600
mids = [(ts, (b + a) / 2.0) for ts, b, a in rows]
basis = ENTRY + fee_per_contract(ENTRY)
hist, i, t, sold = [], 0, math.ceil(BOUGHT), None
while t < close - 8:
    while i < len(rows) and rows[i][0] <= t:
        i += 1
    if i:
        _, b, a = rows[i - 1]
        bid = b if SIDE == "yes" else 1.0 - a
        left = close - t
        hist.append((float(t), bid))
        net = bid - fee_per_contract(bid)
        pnl = (net - basis) / basis
        unit = quoteshock.typical_minute([m for m in mids if m[0] <= t], float(t))
        noise = unit * math.sqrt(SECONDS / 60.0) if unit else 0.0
        held = HOLD and pnl < 0 and net >= FLOOR and unit and (basis - net) < SWING * unit * math.sqrt(left / 60.0)
        if pnl < 0 and bid >= 0.05 and noise > 0 and not held:
            miss = curverisk.stop_miss(net, basis, net, MAX_LOSS)
            if curverisk.squeeze_loss(pnl, miss, net, LINE, MAX_LOSS, noise) and curverisk.still_falling(hist, float(t), SHARE * left, unit):
                sold = (t, bid, net - basis, left, unit)
                break
    t += 1
print(f"{TICKER} {SIDE} bought at {ENTRY} ({basis:.3f} with the fee), swing hold in front: {HOLD}")
if sold:
    t, bid, per, left, unit = sold
    print(f"  the squeeze sells at {bid:.2f} with {left:.0f}s left, {per:+.3f} a contract (typical minute {unit or 0:.3f})")
else:
    print("  the squeeze does not sell")
print("DONE")
