"""What each exit buys and what it costs, in dollars, on every live commodity bet of the last DAYS days. Read-only.

The goal here is not profit but a smaller loss when a bet is going wrong, so every rule is scored on four things: LOSS KEPT (the share of the dollars the losing bets cost that is still lost, 100% = holding), GAVE UP (dollars of winnings given up by selling bets that
would have won), NET (recovered minus gave up, positive = better than holding) and PRICE (cents of winnings given up per dollar of loss recovered, the cost of the insurance). WORST is the biggest single loss left. A sale gets the held bid LAT s after the trigger less
the taker fee. A rule only acts on a bet after its entry. Rules:
    drop D            the bet's bid was at least FAV five seconds ago and has fallen D or more, in the last 180 s (the lane's drop exit; "+c" waits 4 s and needs the bid still 0.10 down)
    bounce D+B        after entry, the bid has fallen D or more under the price paid and has then bounced B or more off its low (the first recovery): sell into the bounce, any time up to 10 s before the close
    lowerhigh L+R     after entry the bid falls L or more off its best, bounces 3c or more but gives back no more than 60% of that fall (a pullback, not a recovery), then turns down again by R off the bounce's high (or makes a new low): the
                      move has resumed (fast move, small pullback, move again), so sell as it resumes, any time up to 10 s before the close
    hover under C     the last 5 s, a steady bid (strategy.price_shape == hover) under both the price paid and C
Exits already in the lane's path are not modeled beyond these.

    scripts/cloud.sh run scripts/exit_tradeoff.py [DAYS=7] [LAT=1]
"""
import os
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract, price_shape

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
DAYS = float(os.environ.get("DAYS", "7"))
LAT = int(os.environ.get("LAT", "1"))
FAV = 0.80

bets = main.db.execute(
    "SELECT d.ts, d.ticker, d.side, d.ask, d.contracts, w.close_ts, w.result FROM decisions d JOIN windows w ON w.ticker = d.ticker "
    "WHERE d.lane='commodity' AND d.action='bet' AND d.mode='trade' AND d.ts > ? AND w.result IN ('yes','no') AND d.contracts > 0 ORDER BY d.ts", (time.time() - DAYS * 86400,)).fetchall()
data = []
for b in bets:
    close = float(b["close_ts"])
    q = [(float(t), float(bid), float(ask)) for t, bid, ask in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (b["ticker"], close - 200, close + 1)).fetchall()]
    if len(q) < 10:
        continue
    pts = [(t, bid if b["side"] == "yes" else 1.0 - ask) for t, bid, ask in q]
    data.append((b, close, pts))


def at(pts, t):
    last = None
    for ts, p in pts:
        if ts > t:
            break
        last = (ts, p)
    return last[1] if last and t - last[0] <= 8 else None


def trigger(b, close, pts, rule):
    kind, a, confirm = rule
    entry = float(b["ts"])
    if kind == "drop":
        for t in range(int(max(close - 180, entry + 6)), int(close - 15)):
            was, now = at(pts, t - 5), at(pts, t)
            if was is None or now is None or was < FAV or was - now < a:
                continue
            if confirm:
                c = at(pts, t + confirm)
                if c is None or c > was - 0.10:
                    return None
                return at(pts, t + confirm + LAT)
            return at(pts, t + LAT)
        return None
    if kind == "lowerhigh":
        peak, low, hi, phase = float(b["ask"]), None, None, 0
        for t in range(int(entry) + 1, int(close - 10)):
            bid = at(pts, t)
            if bid is None or bid <= 0.0:
                continue
            if phase == 0:
                peak = max(peak, bid)
                if peak - bid >= a:
                    phase, low = 1, bid
            elif phase == 1:
                low = min(low, bid)
                if bid - low >= 0.03:
                    phase, hi = 2, bid
            else:
                hi = max(hi, bid)
                if hi - low > 0.6 * (peak - low):
                    peak, phase = hi, 0  # it recovered too far: the fall is over, start again from the new high
                elif bid <= hi - confirm or bid < low:
                    return at(pts, t + LAT) or bid
        return None
    if kind == "bounce":
        low = None
        paid = float(b["ask"])
        for t in range(int(entry) + 1, int(close - 10)):
            bid = at(pts, t)
            if bid is None or bid <= 0.0:
                continue
            low = bid if low is None else min(low, bid)
            if paid - low >= a and bid - low >= confirm:
                return at(pts, t + LAT) or bid
        return None
    for t in range(int(close - 5), int(close)):
        if t <= entry:
            continue
        hist = [x for x in pts if x[0] <= t]
        bid = at(pts, t)
        if bid is None or bid <= 0.0 or bid >= float(b["ask"]) or bid >= a:
            continue
        recent = [p for ts, p in hist if t - 3.0 <= ts <= t and p > 0]
        if len(recent) >= 2 and price_shape(hist, t, bid) == "hover":
            return at(pts, t + LAT) or bid
    return None


def score(rule):
    held_loss = kept = gave = 0.0
    sold = sold_lost = sold_won = 0
    worst = 0.0
    for b, close, pts in data:
        ask, n = float(b["ask"]), float(b["contracts"])
        won = b["result"] == b["side"]
        hold = ((1.0 if won else 0.0) - ask - fee_per_contract(ask)) * n
        px = trigger(b, close, pts, rule)
        sale = (px - ask - fee_per_contract(ask) - fee_per_contract(px)) * n if px is not None and px > 0 else None
        pnl = sale if sale is not None else hold
        if not won:
            held_loss += -hold
            kept += -pnl
        if sale is not None:
            sold += 1
            if won:
                sold_won += 1
                gave += hold - sale
            else:
                sold_lost += 1
        worst = min(worst, pnl)
    recovered = held_loss - kept
    return sold, sold_lost, sold_won, kept / held_loss if held_loss else 0.0, gave, recovered - gave, (gave / recovered * 100 if recovered > 0 else float("nan")), worst


nwin = sum(1 for b, _c, _p in data if b["result"] == b["side"])
held_loss = sum(-(((1.0 if b["result"] == b["side"] else 0.0) - float(b["ask"]) - fee_per_contract(float(b["ask"]))) * float(b["contracts"])) for b, _c, _p in data if b["result"] != b["side"])
print(f"{len(data)} live commodity bets over {DAYS:g} days: {nwin} won, {len(data) - nwin} lost; holding every one, the losers cost ${held_loss:.2f} in all\n")
print(f"{'rule':<22} | {'sold':>4} {'lost':>4} {'won':>4} | {'loss kept':>9} {'gave up':>8} {'NET':>7} {'price':>8} {'worst':>7}")
RULES = [(f"drop {d:.2f}" + ("+c" if c else ""), ("drop", d, c)) for d in (0.10, 0.15, 0.20, 0.30) for c in (0, 4)] + [(f"bounce D{d:.2f} B{bb:.2f}", ("bounce", d, bb)) for d in (0.10, 0.15, 0.25) for bb in (0.02, 0.04)] + [(f"lowerhigh L{d:.2f} R{bb:.2f}", ("lowerhigh", d, bb)) for d in (0.10, 0.15, 0.25) for bb in (0.03, 0.05)] + [(f"hover under {c:.2f}", ("hover", c, 0)) for c in (0.20, 0.30, 0.50)]
for label, rule in RULES:
    sold, sl, sw, kept, gave, net, price, worst = score(rule)
    print(f"{label:<22} | {sold:>4} {sl:>4} {sw:>4} | {kept:>8.0%} {gave:>+8.2f} {net:>+7.2f} {price:>7.0f}c {worst:>+7.2f}")
print("DONE")
