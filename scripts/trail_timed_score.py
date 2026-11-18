"""Would the trail with a time-aware price volatility have beaten the trail with the static one? (2026-11-18, user: "so having a static volatility number is pointless")

One question, same bets (CLAUDE.md rule 4): every bet the trail_timed step (sell step `trail_timed`, pricevol.py, shadow only) wrote a row for in data/risk_shadow.jsonl (kind "trail_timed") where the two verdicts differed (`would_differ`), joined to its own row in decisions and settled, against what actually happened to it (the lane's own sale, or settlement), fees on every leg. The rule, stated before any of these bets: act on the time-aware verdict at the bet's FIRST differing row.
  A. time-aware says sell, static does not: sale at that row's bid less the sell fee, against what happened.
  B. static says sell, time-aware holds: the bet is held to settlement (or to whatever else the lane did) instead of the static trail's sale, against what happened. A bet the static trail did not actually sell (a shadow lane) keeps what happened: no change.
Fees in on every leg. Reports n, the net change in dollars, how many bets each side won, by the time left at the first differing row. Live and paper apart. No market is named in a rule.
    scripts/cloud.sh run scripts/trail_timed_score.py
"""
import datetime
import json
import pathlib

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
TRAIL = "curve risk exit (profit take): trail"
s = config.load()
f = pathlib.Path(s.data_dir) / "risk_shadow.jsonl"
recs = {}
for text in f.read_text().splitlines() if f.exists() else []:
    try:
        x = json.loads(text)
    except ValueError:
        continue
    if x.get("kind") == "trail_timed":
        recs.setdefault((x["ticker"], x["lane"]), []).append(x)
now = datetime.datetime.now(ET)
if not recs:
    print(f"window: none yet. No trail_timed rows in {f} as of {now:%m-%d %H:%M} ET; 0 bets. The step writes one only on a lane whose sell chain names `trail_timed` and whose trail is on: nothing to score.")
    print("DONE")
    raise SystemExit(0)
since = min(x["ts"] for v in recs.values() for x in v)
st = db.connect(s.db_path, "main", shared=True)
results = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (since - 900.0,)).fetchall()}
rows = st.execute("SELECT ts, ticker, lane, mode, side, ask, stake_usd, contracts, exit_price, exit_ts, exit_reason FROM decisions WHERE action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (since - 900.0,)).fetchall()
out, unsettled, seen_keys = [], 0, set()
for r in rows:
    key = (r["ticker"], r["lane"])
    if key not in recs or key in seen_keys:
        continue
    seen_keys.add(key)
    if r["ticker"] not in results:
        unsettled += 1
        continue
    res, close = results[r["ticker"]]
    stake, n0, ask = float(r["stake_usd"]), float(r["contracts"] or 0.0), float(r["ask"] or 0.0)
    basis = stake / n0 if n0 > 0 else 0.0
    if not (ask - 0.06 <= basis <= ask + fee(ask) + 0.06):
        basis = ask + fee(ask)  # decisions.contracts can undercount the fills
    n = stake / basis
    won = res == r["side"]
    sold = r["exit_price"] is not None and r["exit_ts"] is not None and float(r["exit_ts"]) < close - 1
    held = (1.0 if won else 0.0) - basis
    actual = ((float(r["exit_price"]) - fee(float(r["exit_price"])) - basis) if sold else held) * n
    end = float(r["exit_ts"]) if sold else close
    diff = [x for x in recs[key] if float(r["ts"]) < x["ts"] < end and x["side"] == r["side"] and x.get("would_differ")]
    if not diff:
        continue
    g = diff[0]
    if g["hit_timed"] and not g["hit_static"]:
        kind, rule = "A time-aware sells, static does not", (float(g["bid"]) - fee(float(g["bid"])) - basis) * n
    else:
        by_trail = sold and str(r["exit_reason"] or "").startswith(TRAIL)
        kind, rule = "B static sells, time-aware holds", (held * n if by_trail else actual)
    out.append({"r": r, "live": r["mode"] == "trade", "kind": kind, "rule": rule, "actual": actual, "won": won, "sold": sold, "g": g, "stake": stake, "left": float(g["left"])})


def line(name, rs):
    if not rs:
        return
    chg = sum(x["rule"] - x["actual"] for x in rs)
    beat = sum(1 for x in rs if x["rule"] > x["actual"] + 1e-9)
    lost = sum(1 for x in rs if x["rule"] < x["actual"] - 1e-9)
    print(f"  {name:38s} n {len(rs):3d} | what happened ${sum(x['actual'] for x in rs):+8.2f} | with the time-aware verdict ${sum(x['rule'] for x in rs):+8.2f} | net change ${chg:+7.2f} | time-aware better {beat}, static better {lost}, same {len(rs) - beat - lost}")


print(f"window: bets with a trail_timed row where the verdicts differed, first row {datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} ET to {now:%m-%d %H:%M} ET; {len(out)} bets settled and scored, {unsettled} not settled; rows on file for {len(recs)} bets")
if not out:
    print("no bet with a differing row has settled yet: nothing to score.")
for label, live in (("LIVE lanes", True), ("PAPER lanes", False)):
    rs = [x for x in out if x["live"] is live]
    if not rs:
        continue
    print(f"\n=== {label}")
    line("all", rs)
    for k in sorted({x["kind"] for x in rs}):
        print(f"  -- {k}")
        line("all", [x for x in rs if x["kind"] == k])
        for name, lo, hi in (("over 7 min left", 420, 10**9), ("3 to 7 min", 180, 420), ("1.5 to 3 min", 90, 180), ("under 1.5 min", 0, 90)):
            line(name, [x for x in rs if x["kind"] == k and lo <= x["left"] < hi])
    for x in rs:
        r, g = x["r"], x["g"]
        print(f"    {datetime.datetime.fromtimestamp(float(r['ts']), ET):%m-%d %H:%M} {r['ticker']:30s} {r['lane']:20s} {r['side']:3s} paid {float(r['ask']):.2f} | bid {float(g['bid']):.2f}, {g['left']:.0f}s left, units static {g['unit_static']} timed {g['unit_timed']} | {x['kind'][:1]}: ${x['rule']:+.2f} vs actual ${x['actual']:+.2f} ({'sold by the lane' if x['sold'] else 'won' if x['won'] else 'lost'})")
print("DONE")
