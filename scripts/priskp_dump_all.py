import json
from kalshi_moonshot import config, db
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.execute("SELECT d.ts, d.ticker, d.side, d.stake_usd, d.contracts, d.exit_price, d.exit_ts, d.mode, w.result, w.close_ts FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.lane='priskp' AND d.action='bet' AND d.stake_usd>0 AND d.contracts>0 AND w.result IN ('yes','no') ORDER BY d.ts").fetchall()
for t in sorted({r["ticker"] for r in rows}):
    close = [float(r["close_ts"]) for r in rows if r["ticker"] == t][0]
    q = [[float(x[0]), float(x[1]), float(x[2])] for x in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts<? AND bid>0 AND ask>0 ORDER BY ts", (t, close)).fetchall()]
    ix = "KP_" + t.split("-")[0].removeprefix("KX").removesuffix("15M")
    sec = {}
    for r in tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, close - 1600, close + 1)).fetchall():
        sec[int(r[0])] = float(r[1])
    print("DUMP" + json.dumps({"ticker": t, "bets": [dict(r) for r in rows if r["ticker"] == t], "quotes": q, "ticks": [[k, v] for k, v in sorted(sec.items())]}), flush=True)
