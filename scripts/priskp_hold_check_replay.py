"""Would the hold-versus-sell check have held the live PriskP early-take sales, and what did those bets make held to settlement? (2026-11-24; user: "this had plenty of cushion still i don't understand why it sold"; "yes" to scoring the check on the recorded bets before extending it to the fall rule.)
QUESTION (one): for each live priskp bet closed by an early take (fall or stall), at the sale tick: p_lose (the spot's chance of ending on the wrong side: wavetop.p_lose of the index's 1-minute average over the market's own strike, in the index's typical minutes, over the time left) and the hold value 1 - p_lose against what the sale paid after the sale fee. Would the check have held? And, on the same bets, what the bet made as sold (price paid with its buy fee, sale after its fee) against held to settlement (the market's own result).
HONEST LIMITS: a bet the check holds is scored as held to settlement (it could have been sold later by a fall or stall; that is not replayed). The spot is the index's 1-minute average (the live code reads the spot's own average through _wave_spot; this is an approximation). Strike and result come from Kalshi's market record. Nothing was chosen after the results; n is small and today's markets mostly went one way. A lead only.
Run on the host: scripts/cloud.sh run scripts/priskp_hold_check_replay.py"""
import bisect
import datetime
import json
import math
import re
import urllib.request

from kalshi_moonshot import config, db, wavetop
from kalshi_moonshot.kalshiws import index_for_series

s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)


def fee(p):
    return 0.07 * p * (1.0 - p)


def market(t):
    with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{t}", timeout=20) as r:
        return json.load(r)["market"]


rows = st.execute(
    "SELECT ticker, side, ts, stake_usd, contracts, exit_ts, exit_price, exit_reason FROM decisions WHERE lane='priskp' AND action='bet' AND mode='trade' AND exit_ts IS NOT NULL AND exit_reason LIKE 'early take%' AND ts > ? ORDER BY ts",
    (datetime.datetime(2026, 11, 24, 5, tzinfo=datetime.timezone.utc).timestamp(),),
).fetchall()
print(f"{len(rows)} early-take sales")
tot_sold = tot_held = tot_check = 0.0
print(f"{'ticker':27s} {'side':4s} {'paid':>5s} {'sold':>5s} {'kind':>6s} {'left':>5s} {'p_lose':>7s} {'hold':>6s} {'pays':>6s} {'check':>6s} {'result':>6s} {'as sold':>8s} {'held':>7s}")
for r in rows:
    tk, side = r["ticker"], r["side"]
    m = market(tk)
    res, strike = m.get("result"), float(m.get("floor_strike") or 0)
    close_ts = float(st.execute("SELECT close_ts FROM windows WHERE ticker=?", (tk,)).fetchone()["close_ts"]) if st.execute("SELECT 1 FROM windows WHERE ticker=?", (tk,)).fetchone() else None
    if close_ts is None:
        close_ts = datetime.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()
    ser = re.match(r"(KX[A-Z0-9]+15M)", tk).group(1)
    ix = index_for_series(ser)
    tk_rows = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, r["exit_ts"] - 1900.0, r["exit_ts"] + 1.0)).fetchall()
    if len(tk_rows) < 100 or not res or strike <= 0:
        print(tk, "no data")
        continue
    T, V = [float(x[0]) for x in tk_rows], [float(x[1]) for x in tk_rows]
    rets = [math.log(V[i + 1] / V[i]) for i in range(len(V) - 1) if T[i + 1] - T[i] <= 1.5 and V[i] > 0 and T[i] >= r["exit_ts"] - 900.0]
    if len(rets) < 200:
        print(tk, "too few ticks")
        continue
    unit = math.sqrt(sum(x * x for x in rets) / len(rets)) * math.sqrt(60.0)
    j = bisect.bisect_right(T, r["exit_ts"]) - 1
    a = [V[k] for k in range(len(V)) if r["exit_ts"] - 60.0 <= T[k] <= r["exit_ts"]]
    avg = sum(a) / len(a)
    cushion = math.log(avg / strike)
    if side == "no":
        cushion = -cushion
    left = max(close_ts - r["exit_ts"], 1.0)
    lose = wavetop.p_lose(cushion, unit, left)
    pay = float(r["stake_usd"]) / float(r["contracts"])
    sold = float(r["exit_price"])
    pays = sold - fee(sold)
    hold = 1.0 - lose
    check = "HOLD" if pays < hold else "sell"
    won = res == side
    cost = pay + fee(pay)
    as_sold = pays - cost
    held = (1.0 if won else 0.0) - cost
    kind = "fell" if "the bid fell" in r["exit_reason"] else "stall"
    n = float(r["contracts"])
    tot_sold += as_sold * n
    tot_held += held * n
    tot_check += (held if check == "HOLD" else as_sold) * n
    print(f"{tk:27s} {side:4s} {pay:5.2f} {sold:5.2f} {kind:>6s} {left:5.0f} {lose:7.4f} {hold:6.3f} {pays:6.3f} {check:>6s} {res:>6s} {as_sold:+8.3f} {held:+7.3f}")
print(f"\ntotal $ as sold {tot_sold:+.2f}; if every sale had been held to settlement {tot_held:+.2f}; with the check (HOLD -> held to settlement, else as sold) {tot_check:+.2f}")
