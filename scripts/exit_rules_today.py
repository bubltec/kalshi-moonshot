import datetime, sys, traceback
from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee
sys.excepthook = lambda *e: print(''.join(traceback.format_exception(*e)))
ET = datetime.timezone(datetime.timedelta(hours=-5))
s = config.load(); st = db.connect(s.db_path, "main", shared=True); tape = db.connect(s.tape_path, "tape", shared=True)
since = datetime.datetime(2026, 11, 17, 8, 0, tzinfo=ET).timestamp()
rows = st.execute("SELECT ts, ticker, lane, side, stake_usd, contracts, exit_price, exit_ts, exit_reason, close_time FROM decisions WHERE mode='trade' AND action='bet' AND stake_usd>0 AND contracts>0 AND exit_price IS NOT NULL AND ts>? ORDER BY ts", (since,)).fetchall()
tot = {}
for r in rows:
    why = r["exit_reason"] or ""
    kind = "never ahead" if "(never ahead)" in why else "under even" if "(under even)" in why else "trail" if "trail" in why else "profit take" if "profit take" in why else "the 12c line" if why.startswith("curve risk exit:") else why[:24]
    close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp()
    q = tape.execute("SELECT bid, ask FROM quotes WHERE ticker=? AND ts<=? AND bid>0 AND ask>0 ORDER BY ts DESC LIMIT 1", (r["ticker"], close - 1)).fetchone()
    if q is None: continue
    won = (float(q[0]) if r["side"] == "yes" else 1.0 - float(q[1])) > 0.5
    n, cost = float(r["contracts"]), float(r["stake_usd"]); x = float(r["exit_price"])
    got, held = n * (x - fee(x)) - cost, n * (1.0 if won else 0.0) - cost
    t = tot.setdefault(kind, [0, 0, 0.0, 0.0]); t[0] += 1; t[1] += won; t[2] += got; t[3] += held
    print(f"{datetime.datetime.fromtimestamp(float(r['ts']), ET):%H:%M} {r['lane'][:20]:20s} {r['ticker'][:26]:26s} {kind:13s} sold {x:.2f} with {close - float(r['exit_ts']):3.0f}s left ${got:+6.2f} | held ${held:+6.2f} ({'would have won' if won else 'lost anyway'})")
print()
for k, t in sorted(tot.items()):
    print(f"{k:13s} sales {t[0]:2d} | would have won {t[1]:2d} | as sold ${t[2]:+7.2f} | if held ${t[3]:+7.2f} | sale less hold ${t[2] - t[3]:+7.2f}")
