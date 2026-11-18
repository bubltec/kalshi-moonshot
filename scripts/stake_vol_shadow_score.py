"""Would staking in proportion to the market's own typical minute have beaten the flat stake on the live commodity bets? (2026-11-18, user: "and maybe we could scale the bet by lane volatility too"; "Yes, shadow it")

One question on the same bets (CLAUDE.md rule 4): every live bet on a lane with stake_vol_shadow wrote one row (kind "stake_vol" in data/risk_shadow.jsonl) with the market's own typical minute at the buy (sqrt(60) times the rms one-second move of its mid over the last 15 minutes), joined to its own decision row and settled. The rule, stated before any of these bets (studies/results/2026-11-18-commodity-stake-by-volatility.txt): the shadow stake is the flat stake times (the median typical minute of the EARLIER rows over this bet's typical minute), limited to 0.5 to 2. The reference is the earlier rows only, so it is what the live bot could have known; the first 20 rows have no reference and keep multiplier 1.
Per bet: the result per dollar staked as it happened (the sale or settlement, the fee on each leg) and held to settlement; both are scaled by the multiplier. Reports n, net dollars and per $100 staked for flat and scaled, the total staked, and by thirds of the typical minute (the premise: the wild markets are the worse ones). A split by curve shape beyond that comes with more bets. Said so: the cap 0.5 and 2 and the 20-row warm-up are chosen, not fitted.
Run on the host: scripts/cloud.sh run scripts/stake_vol_shadow_score.py"""
import datetime
import json
import pathlib
import statistics

from kalshi_moonshot import config, db

ET = datetime.timezone(datetime.timedelta(hours=-5))
FEE = 0.07
WARM = 20
s = config.load()
f = pathlib.Path(s.data_dir) / "risk_shadow.jsonl"
rows = []
for text in f.read_text().splitlines() if f.exists() else []:
    try:
        x = json.loads(text)
    except ValueError:
        continue
    if x.get("kind") == "stake_vol" and x.get("live"):
        rows.append(x)
now = datetime.datetime.now(ET)
if not rows:
    print(f"window: none yet. No stake_vol rows in {f} as of {now:%m-%d %H:%M} ET; 0 bets. A row is written for each live bet on a lane with stake_vol_shadow when its market has five minutes of quotes: nothing to score.")
    print("DONE")
    raise SystemExit(0)
rows.sort(key=lambda r: r["ts"])
st = db.connect(s.db_path, "main", shared=True)
since = rows[0]["ts"]
results = {w["ticker"]: w["result"] for w in st.execute("SELECT ticker, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (since - 900.0,)).fetchall()}
bets = st.execute("SELECT ts, ticker, lane, side, ask, stake_usd, exit_price, exit_ts FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ask>0 AND ts>?", (since - 60.0,)).fetchall()
by_key = {}
for b in bets:
    by_key.setdefault((b["ticker"], b["lane"], b["side"]), []).append(b)
out, unsettled, unmatched = [], 0, 0
seen_units = []
for r in rows:
    cands = [b for b in by_key.get((r["ticker"], r["lane"], r["side"]), []) if r["ts"] - 5.0 <= float(b["ts"]) <= r["ts"] + 120.0]
    if not cands:
        unmatched += 1
        seen_units.append(r["unit"])
        continue
    b = cands[0]
    res = results.get(r["ticker"])
    ref = statistics.median(seen_units) if len(seen_units) >= WARM else None
    seen_units.append(r["unit"])
    if res is None:
        unsettled += 1
        continue
    ask, stake = float(b["ask"]), float(b["stake_usd"])
    c = stake / ask
    won = res == r["side"]
    held = c * (1 - ask) - c * FEE * ask * (1 - ask) if won else -stake
    if b["exit_ts"] is not None and b["exit_price"] is not None:
        x = max(float(b["exit_price"]), 0.01)
        traded = c * (float(b["exit_price"]) - ask) - c * FEE * x * (1 - x) - c * FEE * ask * (1 - ask)
    else:
        traded = held
    mult = 1.0 if ref is None else min(2.0, max(0.5, ref / r["unit"]))
    out.append({"unit": r["unit"], "stake": stake, "mult": mult, "traded": traded, "held": held, "warm": ref is None})
print(f"window: {datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} to {now:%m-%d %H:%M} ET; {len(rows)} rows, {len(out)} scored ({sum(1 for o in out if o['warm'])} in the warm-up at multiplier 1), {unsettled} not settled yet, {unmatched} without a live bet row.")


def show(label, sel):
    if not sel:
        print(f"  {label:30s} bets 0")
        return
    flat_stake = sum(o["stake"] for o in sel)
    sc_stake = sum(o["stake"] * o["mult"] for o in sel)
    for kind in ("traded", "held"):
        flat = sum(o[kind] for o in sel)
        scaled = sum(o[kind] * o["mult"] for o in sel)
        print(f"  {label:30s} {kind:6s} bets {len(sel):4d} | flat stake ${flat_stake:8.0f} net {flat:+8.2f} ({100.0 * flat / flat_stake:+5.2f} per $100) | scaled stake ${sc_stake:8.0f} net {scaled:+8.2f} ({100.0 * scaled / sc_stake:+5.2f} per $100) | scaled less flat {scaled - flat:+7.2f}")


show("all", out)
if len(out) >= 9:
    units = sorted(o["unit"] for o in out)
    c1, c2 = units[len(units) // 3], units[2 * len(units) // 3]
    show("quietest third (flat view)", [o for o in out if o["unit"] <= c1])
    show("middle third", [o for o in out if c1 < o["unit"] <= c2])
    show("wildest third", [o for o in out if o["unit"] > c2])
print("DONE")
