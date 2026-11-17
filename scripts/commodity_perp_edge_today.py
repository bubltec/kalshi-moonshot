import datetime, re, sys, traceback
from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee
sys.excepthook = lambda *e: print(''.join(traceback.format_exception(*e)))
ET = datetime.timezone(datetime.timedelta(hours=-5))
s = config.load(); st = db.connect(s.db_path, "main", shared=True); tape = db.connect(s.tape_path, "tape", shared=True)
now = datetime.datetime.now().timestamp()
def run(label, since):
    bets = st.execute("SELECT ts, ticker, lane, side, stake_usd, contracts, exit_price, exit_ts, close_time FROM decisions WHERE lane LIKE 'commodity%' AND mode='trade' AND action='bet' AND stake_usd>0 AND contracts>0 AND ts>? ORDER BY ts", (since,)).fetchall()
    g = {"kept (edge -0.05 or better)": [], "refused (edge under -0.05)": [], "no paper reading in 40 s": []}
    for b in bets:
        close = datetime.datetime.fromisoformat(str(b["close_time"]).replace("Z", "+00:00")).timestamp()
        if close > now - 20: continue
        q = tape.execute("SELECT bid, ask FROM quotes WHERE ticker=? AND ts<=? AND bid>0 AND ask>0 ORDER BY ts DESC LIMIT 1", (b["ticker"], close - 1)).fetchone()
        if q is None: continue
        won = (float(q[0]) if b["side"] == "yes" else 1.0 - float(q[1])) > 0.5
        n, cost = float(b["contracts"]), float(b["stake_usd"])
        sold = b["exit_price"] is not None and float(b["exit_ts"]) < close - 1
        actual = (n * (float(b["exit_price"]) - fee(float(b["exit_price"]))) - cost) if sold else n * (1.0 if won else 0.0) - cost
        near = st.execute("SELECT reason FROM decisions WHERE ticker=? AND side=? AND mode<>'trade' AND lane IN ('commodity_perp_check','commodity_multiscale_kalshi','commodity_multiscale_rest') AND ts BETWEEN ? AND ? ORDER BY abs(ts-?) LIMIT 8", (b["ticker"], b["side"], float(b["ts"]) - 40, float(b["ts"]) + 40, float(b["ts"]))).fetchall()
        edge = None
        for x in near:
            m = re.search(r"\[curve edge ([+-][0-9.]+)\]", x["reason"] or "") or re.search(r"is ([0-9.]+) under the ask", x["reason"] or "")
            if m:
                edge = float(m[1]) if m[1][0] in "+-" else -float(m[1]); break
        k = "no paper reading in 40 s" if edge is None else "refused (edge under -0.05)" if edge < -0.05 else "kept (edge -0.05 or better)"
        g[k].append((won, actual, cost))
    print(label)
    for k, v in g.items():
        if v: print(f"  {k:30s} bets {len(v):3d} | won if held {sum(x[0] for x in v):3d} ({100 * sum(x[0] for x in v) / len(v):3.0f}%) | staked ${sum(x[2] for x in v):6.0f} | as traded ${sum(x[1] for x in v):+8.2f}")
run("today since 00:00 ET, live commodity bets on closed markets:", datetime.datetime(2026, 11, 17, 0, 0, tzinfo=ET).timestamp())
run("since 11-13 11:02 ET:", datetime.datetime(2026, 11, 13, 11, 2, tzinfo=ET).timestamp())
