import bisect, datetime, sys, traceback
from kalshi_moonshot import config, db, variants
sys.excepthook = lambda *e: print(''.join(traceback.format_exception(*e)))
ET = datetime.timezone(datetime.timedelta(hours=-5))
s = config.load(); st = db.connect(s.db_path, "main", shared=True); tape = db.connect(s.tape_path, "tape", shared=True)
hm = lambda t: datetime.datetime.fromtimestamp(t, ET).strftime("%H:%M:%S")
close = datetime.datetime(2026, 11, 17, 13, 45, tzinfo=ET).timestamp(); opened = close - 900
for tk, kp in (("KXSILVER15M-26NOV171345-45", "KP_SILVER"), ("KXGOLD15M-26NOV171345-45", "KP_GOLD")):
    r = st.execute("SELECT ts, side, ask, reason FROM decisions WHERE ticker=? AND mode='trade' AND action='bet' ORDER BY ts LIMIT 1", (tk,)).fetchone()
    t0 = float(r["ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (tk, opened - 20, close)).fetchall()
    qt = [float(x[0]) for x in q]; mids = [(float(x[0]), (float(x[1]) + float(x[2])) / 2) for x in q]
    def at(t, col):
        i = bisect.bisect_right(qt, t) - 1
        return float(q[i][col]) if i >= 0 else float('nan')
    print(f"\n{tk} YES bought {hm(t0)} ({(t0 - opened) / 60:.1f} min in) decision ask {float(r['ask']):.2f}")
    print("  note: " + (r["reason"] or "")[:300])
    print(f"  crossings since the open {variants.quote_crossings(mids, opened, t0)} | led {variants.quote_led_share(mids, 'yes', opened, t0)} | wave {variants.quote_wave_features(mids, 'yes', opened, t0)}")
    print("  YES bid/ask each 5 s, last 60 s before the buy: " + " ".join(f"{at(t0 - k, 1):.2f}/{at(t0 - k, 2):.2f}" for k in range(60, -1, -5)))
    print("  YES bid each 15 s from the open (| = buy): " + " ".join(("| " if opened + k <= t0 < opened + k + 15 else "") + f"{at(opened + k, 1):.2f}" for k in range(15, int(min(close, datetime.datetime.now().timestamp()) - opened), 15)))
    p = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (kp, opened - 5, close)).fetchall()
    PT = [float(x[0]) for x in p]; PV = [float(x[1]) for x in p]
    ref = PV[bisect.bisect_left(PT, opened)]
    w = st.execute("SELECT strike FROM windows WHERE ticker=?", (tk,)).fetchone()
    print(f"  perp at the open {ref} (window strike {w['strike'] if w else None}); perp bp from the open each 30 s (| = buy): " + " ".join(("| " if opened + k <= t0 < opened + k + 30 else "") + f"{(PV[max(bisect.bisect_right(PT, opened + k) - 1, 0)] / ref - 1) * 1e4:+.1f}" for k in range(0, int(min(close, datetime.datetime.now().timestamp()) - opened), 30)))
    for n in st.execute("SELECT lane, action, ts, reason FROM decisions WHERE ticker=? AND mode<>'trade' AND lane IN ('commodity_perp_check','commodity_multiscale_kalshi') AND ts BETWEEN ? AND ? ORDER BY ts", (tk, t0 - 60, t0 + 60)).fetchall():
        print(f"  paper {n['lane']} {hm(float(n['ts']))} {n['action']}: {(n['reason'] or '')[:150]}")
