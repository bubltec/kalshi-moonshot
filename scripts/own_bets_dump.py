"""THE BOT'S OWN BETS, one row each, with what the buy logic recorded (2026-11-21, user: "It's supposed to be comparing our wins and losses and the features inside our logic"; "There's like 13 features").
Window: decisions with action 'bet' and a stake from Mon 2026-11-09 05:00 UTC on (the logic was rewritten that Monday), live (mode trade) and paper (observe), settled. No replay and no new reading: each row is the bet as it was placed, its own decision note (what every step of the lane's buy module read and said), the factors logged at the buy, and how it really ended (its own sale or settlement, fee on both legs, the bet's own contracts).
For the live commodity bets since 2026-11-19 00:00 ET (the present buy module) it also writes the raw perp ticks (35 minutes to the close) and Kalshi quotes of the market, so each step's reading can be recomputed by hand.
Writes own_bets.json.gz and own_bets_raw.json.gz to DATA_DIR (returned in job-results/<id>/files). Run: SPOT=0 scripts/cloud_job.sh scripts/own_bets_dump.py"""
import datetime, gzip, json, os
from pathlib import Path
from kalshi_moonshot import config, db
from kalshi_moonshot.perpref import index_for_series as perp_index
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
T0 = datetime.datetime(2026, 11, 9, 5, tzinfo=datetime.timezone.utc).timestamp()
RAW0 = datetime.datetime(2026, 11, 19, 5, tzinfo=datetime.timezone.utc).timestamp()
W = {w["ticker"]: w for w in st.execute("SELECT ticker, series, close_ts, strike, result, settled_value FROM windows WHERE close_ts>?", (T0,)).fetchall()}
rows, raw = [], {}
for r in st.execute("SELECT id, ts, ticker, lane, mode, side, bid, ask, model_p, cal_p, edge, reason, outcome, exit_price, exit_ts, exit_reason, contracts, stake_usd, lane_ver, factors, exit_factors, version FROM decisions WHERE action='bet' AND stake_usd>0 AND contracts>0 AND ts>? AND outcome IN ('yes','no') ORDER BY ts", (T0,)).fetchall():
    n, w = float(r["contracts"]), W.get(r["ticker"])
    basis = float(r["stake_usd"]) / n
    close = float(w["close_ts"]) if w else None
    sold = r["exit_price"] is not None and r["exit_ts"] is not None and (close is None or float(r["exit_ts"]) < close - 1)
    won = r["outcome"] == r["side"]
    per = ((float(r["exit_price"]) - fee(float(r["exit_price"]))) if sold else (1.0 if won else 0.0)) - basis
    rows.append({"id": r["id"], "ts": r["ts"], "ticker": r["ticker"], "lane": r["lane"], "mode": r["mode"], "side": r["side"], "bid": r["bid"], "ask": r["ask"], "model_p": r["model_p"], "cal_p": r["cal_p"], "edge": r["edge"],
                 "reason": r["reason"], "won": won, "sold": sold, "exit_price": r["exit_price"] if sold else None, "exit_ts": r["exit_ts"] if sold else None, "exit_reason": r["exit_reason"] if sold else None,
                 "contracts": n, "stake": r["stake_usd"], "paid": basis, "net": per * n, "net_per_dollar": per / basis, "hold_per_dollar": ((1.0 if won else 0.0) - basis) / basis, "lane_ver": r["lane_ver"], "version": r["version"],
                 "factors": json.loads(r["factors"]) if r["factors"] else None, "exit_factors": json.loads(r["exit_factors"]) if r["exit_factors"] else None,
                 "close": close, "strike": w["strike"] if w else None, "settled_value": w["settled_value"] if w else None, "series": w["series"] if w else None})
    if r["mode"] == "trade" and r["lane"].startswith("commodity") and r["ts"] > RAW0 and w and r["ticker"] not in raw:
        ix = perp_index(w["series"])
        tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, close - 2100, close + 5)).fetchall() if ix else []
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (r["ticker"], close - 960, close + 5)).fetchall()
        raw[r["ticker"]] = {"index": ix, "ticks": [(round(float(a), 2), float(b)) for a, b in tk], "quotes": [(round(float(a), 2), float(b), float(c)) for a, b, c in q]}
out = Path(os.environ.get("DATA_DIR", "/data"))
with gzip.open(out / "own_bets.json.gz", "wt") as f: json.dump(rows, f)
with gzip.open(out / "own_bets_raw.json.gz", "wt") as f: json.dump(raw, f)
print(f"window: bets from 2026-11-09 05:00 UTC to {datetime.datetime.fromtimestamp(max(x['ts'] for x in rows), datetime.timezone.utc):%m-%d %H:%M} UTC; {len(rows)} settled bets with a stake ({sum(1 for x in rows if x['mode'] == 'trade')} live, {sum(1 for x in rows if x['mode'] != 'trade')} paper); raw ticks and quotes for {len(raw)} live commodity markets since 11-19")
print("sizes:", [(p.name, p.stat().st_size) for p in out.glob("own_bets*")])
