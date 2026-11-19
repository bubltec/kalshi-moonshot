"""Would selling a losing bet whose path since the buy is a trend against it, or flat, have beaten what the bet did? (2026-11-18, user: "if the path is wavy it means there's likely a comeback"; "if look back is trend or flat, and we're losing, we should probably exit"; "we need to test these concepts")

One question, same bets (CLAUDE.md rule 4): every bet the shape step (sell step `shape`, pathshape.py, shadow only) wrote a row for in data/risk_shadow.jsonl (kind "shape"), joined to its own row in decisions and settled, against what actually happened to it (the lane's own sale, or settlement), fees on every leg. The rule, stated before any of these bets: sell at the bid of the bet's FIRST row with would_sell true (the since-the-buy shape is trend_against or flat while the bet is behind), less the sell fee. A bet never flagged keeps what happened. Split by the shape on each look-back at that first flag (since the buy; a quarter of the time left), and, for the bets never flagged, by the shapes they showed. Live and paper apart. No market is named in a rule.
    scripts/cloud.sh run scripts/shape_shadow_score.py
"""
import datetime
import json
import os
import pathlib

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
KIND = os.environ.get("KIND", "shape")  # "shape_timed": score the time-based step (its `buy` is the shape over its window, `quarter` the shape since the buy)
SHAPES = [x for x in os.environ.get("SHAPES", "").split(",") if x]
ONLY = os.environ.get("LANE")  # optional: score one lane, e.g. LANE=crypto_risk_score
s = config.load()
f = pathlib.Path(s.data_dir) / "risk_shadow.jsonl"
recs = {}
for text in f.read_text().splitlines() if f.exists() else []:
    try:
        x = json.loads(text)
    except ValueError:
        continue
    if x.get("kind") == KIND:
        recs.setdefault((x["ticker"], x["lane"]), []).append(x)
now = datetime.datetime.now(ET)
if not recs:
    print(f"window: none yet. No shape rows in {f} as of {now:%m-%d %H:%M} ET; 0 bets. The step writes one only for a losing bet on a lane whose sell chain names `shape`: nothing to score.")
    print("DONE")
    raise SystemExit(0)
since = min(x["ts"] for v in recs.values() for x in v)
st = db.connect(s.db_path, "main", shared=True)
results = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (since - 900.0,)).fetchall()}
rows = st.execute("SELECT ts, ticker, lane, mode, side, ask, stake_usd, contracts, exit_price, exit_ts, exit_reason FROM decisions WHERE action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (since - 900.0,)).fetchall()
rows = [r for r in rows if not ONLY or r["lane"] == ONLY]
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
    actual = ((float(r["exit_price"]) - fee(float(r["exit_price"])) if sold else (1.0 if won else 0.0)) - basis) * n
    end = float(r["exit_ts"]) if sold else close
    mine = [x for x in recs[key] if float(r["ts"]) < x["ts"] < end and x["side"] == r["side"]]
    if not mine:
        continue
    flag = next((x for x in mine if x.get("would_sell") and (not SHAPES or x["buy"]["shape"] in SHAPES)), None)  # SHAPES=trend_against: the variant that waits for that shape (a rule chosen after seeing the results)
    rule = ((float(flag["bid"]) - fee(float(flag["bid"])) - basis) * n) if flag else None
    out.append({"r": r, "live": r["mode"] == "trade", "won": won, "sold": sold, "actual": actual, "rule": rule, "flag": flag, "stake": stake,
                "buy": flag["buy"]["shape"] if flag else None, "quarter": flag["quarter"]["shape"] if flag else None,
                "shown": sorted({x["buy"]["shape"] for x in mine})})


def line(name, rs):
    if not rs:
        return
    a = sum(x["actual"] for x in rs)
    b = sum(x["rule"] if x["rule"] is not None else x["actual"] for x in rs)
    tk = [x for x in rs if x["rule"] is not None]
    beat = sum(1 for x in tk if x["rule"] > x["actual"] + 1e-9)
    lost = sum(1 for x in tk if x["rule"] < x["actual"] - 1e-9)
    print(f"  {name:44s} n {len(rs):3d} | staked ${sum(x['stake'] for x in rs):8.2f} | won if held {sum(1 for x in rs if x['won']):3d} | what happened ${a:+8.2f} | with the rule ${b:+8.2f} | change ${b - a:+7.2f} | would sell {len(tk):2d}: beat {beat}, lost to {lost}")


first_flat = [x for x in out if x["buy"] == "flat"]
print(f"flat at the first would-sell: {len(first_flat)} bets, of which {sum(1 for x in first_flat if 'trend_against' in x['shown'])} later showed a trend_against row while behind")
print(f"window: bets with a shape row, first row {datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} ET to {now:%m-%d %H:%M} ET; {len(out)} bets settled and scored, {unsettled} not settled; shape rows on file for {len(recs)} bets")
if not out:
    print("no bet with a shape row has settled yet: nothing to score.")
for label, live in (("LIVE lanes", True), ("PAPER lanes", False)):
    rs = [x for x in out if x["live"] is live]
    if not rs:
        continue
    print(f"\n=== {label}")
    line("all", rs)
    print("  -- the rule's sales, by the shape since the buy at the first would-sell")
    for k in ("trend_against", "flat"):
        line(k, [x for x in rs if x["buy"] == k])
    print("  -- the same sales, by the shape over a quarter of the time left")
    for k in ("trend_against", "flat", "wavy", "trend_for", "anomaly", "unknown"):
        line(k, [x for x in rs if x["flag"] and x["quarter"] == k])
    print("  -- never flagged (the rule holds, so no change), by the shapes the bet showed since the buy while behind")
    never = [x for x in rs if not x["flag"]]
    for k in sorted({"+".join(x["shown"]) for x in never}):
        line(k, [x for x in never if "+".join(x["shown"]) == k])
    for x in rs:
        if x["flag"]:
            r, g = x["r"], x["flag"]
            print(f"    {datetime.datetime.fromtimestamp(float(r['ts']), ET):%m-%d %H:%M} {r['ticker']:30s} {r['lane']:20s} {r['side']:3s} paid {float(r['ask']):.2f} | since buy {x['buy']}, quarter {x['quarter']} | rule sells {float(g['bid']):.2f} with {g['left']:.0f}s left ${x['rule']:+.2f} | actual ${x['actual']:+.2f} ({'sold by the lane' if x['sold'] else 'won' if x['won'] else 'lost'})")

print("\n=== the same, split by the window the bet was in (a study's cut, after seeing the results): WAVE window = 5 or more scored bets in one 15-minute window, 60% or more of them ended behind (what happened below zero); OTHER = every other window. All scored bets, and the bets from 11-18 18:31 ET on (the push) apart.")
by_win = {}
for x in out:
    by_win.setdefault(x["r"]["ticker"].split("-")[1], []).append(x)
wave_wins = {w for w, xs in by_win.items() if len(xs) >= 5 and sum(1 for y in xs if y["actual"] < 0) >= 0.6 * len(xs)}
for label, since_ts in (("all scored bets", 0), ("since the push (11-18 18:31 ET)", 1795044681)):
    sub = [x for x in out if float(x["r"]["ts"]) >= since_ts]
    wv = [x for x in sub if x["r"]["ticker"].split("-")[1] in wave_wins]
    ot = [x for x in sub if x["r"]["ticker"].split("-")[1] not in wave_wins]
    print(f"  -- {label}: {len(wave_wins)} wave windows in the whole set")
    line("wave windows", wv)
    line("other windows", ot)
    for sh in ("trend_against", "flat"):
        line(f"  wave windows, first would-sell {sh}", [x for x in wv if x["flag"] and x["buy"] == sh])
        line(f"  other windows, first would-sell {sh}", [x for x in ot if x["flag"] and x["buy"] == sh])

print("\n=== flat and trend_against sales by the CUSHION at the flag (a study's cut, after seeing the results; user, 2026-11-19: \"theres factors to consider when flat\"): cushion = the risk score's chance the side pays at the flag (the row's q) less what the sale pays, the bid net of its fee. Positive = the model says the bet is worth more held than sold (a cushion under it); negative = the sale pays more than the model thinks the bet is worth (little or no cushion). Thirds of the cushion over all flagged bets, then the sign.")
fl = [x for x in out if x["flag"]]
cu = lambda x: float(x["flag"]["q"]) - (float(x["flag"]["bid"]) - fee(float(x["flag"]["bid"])))  # noqa: E731
if len(fl) >= 30:
    cuts = sorted(cu(x) for x in fl)
    c1, c2 = cuts[len(cuts) // 3], cuts[2 * len(cuts) // 3]
    print(f"  cushion thirds cut at {c1:+.3f} and {c2:+.3f} (all {len(fl)} flagged bets)")
    for shp in ("flat", "trend_against"):
        sub = [x for x in fl if x["buy"] == shp]
        print(f"  -- first would-sell {shp}: {len(sub)} bets")
        line("  cushion lowest third (sale pays more)", [x for x in sub if cu(x) <= c1])
        line("  cushion middle third", [x for x in sub if c1 < cu(x) <= c2])
        line("  cushion highest third (model says hold)", [x for x in sub if cu(x) > c2])
        line("  cushion under zero", [x for x in sub if cu(x) < 0])
        line("  cushion zero or over", [x for x in sub if cu(x) >= 0])
    print("  -- by the seconds left at the flag, flat only: over 600 | 300 to 600 | under 300")
    sub = [x for x in fl if x["buy"] == "flat"]
    line("  flat, over 600 s left", [x for x in sub if float(x["flag"]["left"]) > 600])
    line("  flat, 300 to 600 s left", [x for x in sub if 300 < float(x["flag"]["left"]) <= 600])
    line("  flat, under 300 s left", [x for x in sub if float(x["flag"]["left"]) <= 300])
print("DONE")
