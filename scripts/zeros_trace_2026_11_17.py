import bisect, datetime, sys, traceback
sys.excepthook = lambda *e: print(''.join(traceback.format_exception(*e)))
from kalshi_moonshot import config, db, variants
ET = datetime.timezone(datetime.timedelta(hours=-5))
s = config.load(); st = db.connect(s.db_path, "main", shared=True); tape = db.connect(s.tape_path, "tape", shared=True)
since = datetime.datetime(2026, 11, 17, 9, 20, tzinfo=ET).timestamp()
rows = st.execute("SELECT ts, ticker, lane, side, ask, stake_usd, contracts, exit_price, exit_ts, exit_reason, close_time, reason FROM decisions WHERE mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (since,)).fetchall()
hm = lambda t: datetime.datetime.fromtimestamp(t, ET).strftime("%H:%M:%S")
for r in rows:
    close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp()
    opened, t0, side = close - 900.0, float(r["ts"]), r["side"]
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], opened - 20, close + 5)).fetchall()
    qt = [float(x[0]) for x in q]
    mids = [(float(x[0]), (float(x[1]) + float(x[2])) / 2) for x in q]
    def bid_at(t):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0: return None
        return float(q[i][1]) if side == "yes" else 1.0 - float(q[i][2])
    last = bid_at(close - 1)
    print(f"\n{r['ticker']} {r['lane']} {side.upper()} bought {hm(t0)} ({(t0 - opened) / 60:.1f} min in, {close - t0:.0f}s left) paid {float(r['stake_usd']) / float(r['contracts']):.3f} (ask {float(r['ask']):.2f}); last bid before the close {last}")
    print(f"  crossings of even since the open at the buy: {variants.quote_crossings(mids, opened, t0)} | share led: {variants.quote_led_share(mids, side, opened, t0)} | wave: {variants.quote_wave_features(mids, side, opened, t0)}")
    print("  buy note: " + (r["reason"] or "")[:300])
    print("  side's bid every 15 s from the open (| marks the buy): " + " ".join(("| " if opened + k <= t0 < opened + k + 15 else "") + (f"{b:.2f}" if (b := bid_at(opened + k)) is not None else "--") for k in range(0, 900, 15)))
    # the fall: first second after the buy the bid was under 0.70, 0.50, 0.30
    marks = []
    for lvl in (0.80, 0.70, 0.50, 0.30, 0.15):
        t = next((tt for tt in range(int(t0) + 1, int(close)) if bid_at(tt) is not None and bid_at(tt) < lvl), None)
        marks.append(f"under {lvl:.2f} at {hm(t)} ({close - t:.0f}s left)" if t else f"never under {lvl:.2f}")
    print("  after the buy: " + "; ".join(marks))
    if r["exit_price"] is not None:
        print(f"  sold {float(r['exit_price']):.2f} at {hm(float(r['exit_ts']))} ({close - float(r['exit_ts']):.0f}s left): {(r['exit_reason'] or '')[:230]}")
    # the under-even rule of this morning's study: under 0.50 without a break for a tenth of the time left; sold at the worse of the next two bids
    from kalshi_moonshot.strategy import fee_per_contract as fee
    n = float(r["contracts"]); cost = float(r["stake_usd"]); since_u = None; sale = None
    stop = float(r["exit_ts"]) if r["exit_price"] is not None else close
    for tt in range(int(t0) + 1, int(stop) - 1):
        b = bid_at(tt); left = close - tt
        if b is None: continue
        if b < 0.50:
            since_u = since_u if since_u is not None else tt
            if tt - since_u >= 0.10 * left and b >= 0.05:
                f = min(x for x in (bid_at(tt + 1), bid_at(tt + 2)) if x is not None)
                sale = (f, left, n * (f - fee(f)) - cost); break
        else:
            since_u = None
    settled = 1.0 if (last or 0) > 0.5 else 0.0
    actual = (n * (float(r["exit_price"]) - fee(float(r["exit_price"]))) - cost) if r["exit_price"] is not None else n * settled - cost
    print(f"  RESULT actual ${actual:+.2f} | under-even rule: " + (f"sells {sale[0]:.2f} with {sale[1]:.0f}s left, ${sale[2]:+.2f} (change ${sale[2] - actual:+.2f})" if sale else "does not sell"))
