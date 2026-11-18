"""Would deciding again after a missed resting buy have beaten what the live lane did? (2026-11-18, user: "if it doesn't fill 1-2c under in a few seconds, shouldn't another tick have gone by with another choice?"; "Yes, shadow option")

One question on the same bets (CLAUDE.md rule 4): every live resting buy that rested its time and filled nothing, on a lane with buy_redecide_shadow, wrote one row (kind "redecide" in data/risk_shadow.jsonl): what a fresh decision at the market's price then would have done, beside what the live lane did. The rule, stated before any of these rows: the shadow acts on its fresh decision ONCE (no further re-decisions are simulated): if it says bet, a bid rests at its `would_rest` price (1c under the ask then, no fee) for the lane's own rest time from the row's time, and fills when the side's ask comes down to it or a taker trades at or through it on Kalshi's tape (the kind fill, the best case for a resting bid); a bid that does not fill is no bet. A filled bid and the live lane's buy are both held to settlement, so the question is only the buy: its price, its fee, and whether the market was still one the lane wants.
  live    the live lane's result on the bet it made after the miss (it took the ask: cost per contract includes the fee), or nothing when it bought nothing
  shadow  the re-decision's result on its own resting bid, no fee
Reports n rows, how many the fresh decision said bet to and how many of those filled, net dollars per contract for each side on the same rows, and the rows the two did differently. By what the live lane did (took the ask, bought nothing). A split by curve shape comes when there are enough rows to split (rule 4: a positive result in each shape before it goes live).
Run on the host: scripts/cloud.sh run scripts/redecide_shadow_score.py"""
import datetime
import json
import pathlib

from kalshi_moonshot import config, db

ET = datetime.timezone(datetime.timedelta(hours=-5))
s = config.load()
f = pathlib.Path(s.data_dir) / "risk_shadow.jsonl"
rows = []
for text in f.read_text().splitlines() if f.exists() else []:
    try:
        x = json.loads(text)
    except ValueError:
        continue
    if x.get("kind") == "redecide" and x.get("live"):
        rows.append(x)
now = datetime.datetime.now(ET)
if not rows:
    print(f"window: none yet. No live redecide rows in {f} as of {now:%m-%d %H:%M} ET; 0 rows. A row is written only when a live lane with buy_redecide_shadow rests a buy and it fills nothing: nothing to score.")
    print("DONE")
    raise SystemExit(0)
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
since = min(x["ts"] for x in rows)
results = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (since - 900.0,)).fetchall()}
rest = {}  # the lane's own rest time
from kalshi_moonshot import lanes  # noqa: E402

for name, spec in lanes.lane_specs(s).items():
    rest[name] = float(spec.buy_rest_seconds or 5.0)
done, unsettled = [], 0
for x in rows:
    if x["ticker"] not in results:
        unsettled += 1
        continue
    res, close = results[x["ticker"]]
    won = res == x["side"]
    live = ((1.0 if won else 0.0) - x["live_cost"] / x["live_filled"]) if x["live_filled"] > 0 else 0.0
    shadow, filled = 0.0, False
    if x["would"] == "bet" and x.get("would_rest"):
        p, t0, t1 = float(x["would_rest"]), float(x["ts"]), float(x["ts"]) + rest.get(x["lane"], 5.0)
        side = x["side"]
        for ts_, bid, ask in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid>0 AND ask>0 ORDER BY ts", (x["ticker"], t0, t1)).fetchall():
            a = float(ask) if side == "yes" else 1.0 - float(bid)
            if a <= p + 1e-9:
                filled = True
                break
        if not filled:
            for ts_, yv, nv, vw in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts>? AND ts<=?", (x["ticker"], int(t0), int(t1))).fetchall():
                if vw is None:
                    continue
                if (side == "yes" and nv > 0 and vw <= p + 1e-9) or (side == "no" and yv > 0 and 1.0 - vw <= p + 1e-9):
                    filled = True
                    break
        shadow = ((1.0 if won else 0.0) - p) if filled else 0.0
    done.append((x, live, shadow, filled))
print(f"window: {datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} to {now:%m-%d %H:%M} ET; {len(rows)} live rows, {len(done)} scored, {unsettled} not settled yet. One contract a row; both held to settlement; the live lane's cost includes its fee, the shadow's resting bid has none.")


def show(label, sel):
    if not sel:
        print(f"  {label:34s} rows 0")
        return
    bets = [d for d in sel if d[0]["would"] == "bet"]
    filled = [d for d in bets if d[3]]
    live_n = [d for d in sel if d[0]["live_filled"] > 0]
    lt, st_ = sum(d[1] for d in sel), sum(d[2] for d in sel)
    diff = sum(1 for d in sel if abs(d[1] - d[2]) > 1e-9)
    print(f"  {label:34s} rows {len(sel):4d} | live bought {len(live_n):4d} net {lt:+8.2f} | fresh decision said bet {len(bets):4d}, filled {len(filled):4d}, net {st_:+8.2f} | shadow less live {st_ - lt:+8.2f} | rows that differ {diff:4d}")


show("all", done)
show("the live lane took the ask", [d for d in done if d[0]["live_filled"] > 0])
show("the live lane bought nothing", [d for d in done if d[0]["live_filled"] <= 0])
print("DONE")
