"""What the ladder's chance would have been on the live lane's own bets with the width read from the last hour only (2026-11-14).
Live until then: the width was six hours of 5-minute bars times a multiplier refitted hourly on six hours of outcomes. New: the root-mean-square of the perp's one-minute moves over the last hour, over the time left, no multiplier.
For each crypto_multiscale bet since START (ET, default 10:00 today): the distance to the strike is recovered from the lane's own logged chance (model_p) and width (ln distance = z x width; both are logged rounded, the width to 0.01%, so the recovered distance is approximate), the hour's rate is read from the taped index at the bet's time, and the chance, the edge after the fee and the scaled edge bar (min_edge_room, 0.10 today) are recomputed.
Same bets, compared with what happened (sold or settled, fee on every leg). The bets are the ones the old width chose, so this shows which it would NOT have bought, never which others it would have. Run on the host: scripts/cloud.sh run scripts/hour_width_replay.py"""
import bisect
import math
import os
import re
from statistics import NormalDist

from kalshi_moonshot import config, db
from kalshi_moonshot.commodities import instrument_for, load_instruments
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

N = NormalDist()
ROOM = float(os.environ.get("ROOM", "0.10"))
UNC = 0.00015  # the floor on the uncertainty of the center (commodity_window_unc_floor)
s = config.load()
store = Store(s.db_path)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = store.db.execute(
    "SELECT id, ts, ticker, side, ask, contracts, stake_usd, exit_price, outcome, reason, model_p FROM decisions WHERE lane='crypto_multiscale' AND action='bet' AND mode='trade' AND ts > ? ORDER BY ts",
    (float(os.environ.get("SINCE", "1794668400")),),  # 2026-11-14 10:00 ET
).fetchall()


def rate_at(index_id, now):
    got = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (index_id, now - 3700.0, now)).fetchall()
    ts, vs = [float(r[0]) for r in got], [float(r[1]) for r in got]
    levels = []
    for k in range(61):
        i = bisect.bisect_right(ts, now - 60.0 * k) - 1
        if i < 0:
            break
        levels.append(math.log(vs[i]))
    moves = [levels[k] - levels[k + 1] for k in range(len(levels) - 1)]
    return math.sqrt(sum(m * m for m in moves) / len(moves) / 60.0) if len(moves) >= 30 else None


tot = {"n": 0, "kept": 0, "pnl_all": 0.0, "pnl_kept": 0.0, "pnl_dropped": 0.0}
print("time     market   side ask   old   new   edge  bar   buy?  result")
for r in rows:
    why = r["reason"] or ""
    m = re.search(r"([0-9.]+)h left, width ([0-9.]+)%", why)  # the stored note is cut short before its P(yes), so the chance comes from the model_p column (the side's own chance; the record adjustment is off on this lane)
    inst = instrument_for(r["ticker"].split("-")[0], load_instruments(s.data_dir))
    if not m or inst is None or not inst.symbol.startswith("kp:"):
        continue
    hours, width_old = float(m.group(1)), float(m.group(2)) / 100.0
    p_side_old = min(max(float(r["model_p"] or 0.5), 1e-4), 1 - 1e-4)
    p_yes_old = p_side_old if r["side"] == "yes" else 1.0 - p_side_old
    rate = rate_at(inst.symbol.removeprefix("kp:"), float(r["ts"]))
    if rate is None:
        continue
    lnd = N.inv_cdf(p_yes_old) * width_old
    width_new = math.sqrt((rate * math.sqrt(hours * 3600.0)) ** 2 + UNC ** 2)
    p_yes_new = N.cdf(lnd / width_new)
    side, ask = r["side"], float(r["ask"])
    p_old = p_yes_old if side == "yes" else 1 - p_yes_old
    p_new = p_yes_new if side == "yes" else 1 - p_yes_new
    edge = p_new - ask - fee_per_contract(ask)
    room = 1.0 - ask - fee_per_contract(ask)
    bar = ROOM * room
    buy = edge >= bar
    n, stake = float(r["contracts"] or 0), float(r["stake_usd"] or 0)
    if r["exit_price"] is not None:
        x = float(r["exit_price"])
        pnl, how = n * (x - fee_per_contract(x)) - stake, f"sold {x:.2f}"
    elif r["outcome"] in ("yes", "no"):
        pnl, how = (n if r["outcome"] == side else 0.0) - stake, "won" if r["outcome"] == side else "lost"
    else:
        continue
    tot["n"] += 1
    tot["pnl_all"] += pnl
    tot["kept" if buy else "n"] += 1 if buy else 0
    tot["pnl_kept" if buy else "pnl_dropped"] += pnl
    import datetime
    t = datetime.datetime.fromtimestamp(float(r["ts"]), datetime.timezone(datetime.timedelta(hours=-5))).strftime("%H:%M:%S")
    print(f"{t} {r['ticker'][2:5]:4s} {r['ticker'][-12:-8]} {side:3s} {ask:.2f}  {p_old:.3f} {p_new:.3f} {edge:+.3f} {bar:.3f} {'BUY ' if buy else 'pass'}  {how} {pnl:+.2f}  (width {width_old*1e4:.0f} -> {width_new*1e4:.0f} bp)")
print(f"bets {tot['n']}: what happened {tot['pnl_all']:+.2f} | still bought {tot['kept']}: {tot['pnl_kept']:+.2f} | not bought {tot['n'] - tot['kept']}: {tot['pnl_dropped']:+.2f} (the money the new width would have left on or off the table)")
print("DONE")
