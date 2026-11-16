"""When does the model's edge screen help? (2026-11-16, user: "the always = true works sometimes and sometimes it doesn't. we should figure out how that variable can be automated and when we should trust the outcomes of when it is on vs when it is not"; "lets re-work that concept. because 'always' means nothing")

`always = true` switches off one thing: the screen that asks the ladder's own chance to beat the price (edge / (1 - ask - fee) >= min_edge_room, 0.10 on the live lane). It has been on for the live crypto lane since 2026-11-15 16:31 ET, and every bet since then still records the ladder's chance and edge. So each bet can be labelled by what the screen WOULD have done.
One question on the lane's own bets (CLAUDE.md rule 4), from the decisions table only (the ask, the ladder's edge and the stake as recorded at the buy; the sale or the settlement after it; nothing joined in from another feed):
  1. bets the screen would have passed against bets it would have dropped: held to settlement and as traded
  2. the same by the size of the edge
  3. by six-hour block: does the screen help in some stretches and hurt in others?
  4. can its own recent record say when to trust it? Before each bet, the average held result of the previous 20 bets the screen would have DROPPED: when that record is positive (dropped bets have been paying), did the next dropped bet pay? And the same for passed bets.

Run on the host: scripts/cloud.sh run scripts/edge_screen_record.py
"""
import datetime
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 15, 16, 31, tzinfo=ET).timestamp()
ROOM = 0.10
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
res = {w["ticker"]: w["result"] for w in st.execute("SELECT ticker, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE - 900,)).fetchall()}
rows, seen = [], set()
for d in st.execute("SELECT ts, ticker, side, ask, model_p, edge, stake_usd, exit_price FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall():
    k = (d["ticker"], d["side"])
    if k in seen or d["ticker"] not in res or d["ask"] is None or d["model_p"] is None:
        continue
    seen.add(k)
    a = float(d["ask"])
    basis = a + fee(a)
    if not (0.05 < a < 0.99) or basis >= 1:
        continue
    edge = float(d["model_p"]) - basis
    won = res[d["ticker"]] == d["side"]
    stake = float(d["stake_usd"])
    x = d["exit_price"]
    traded = stake / basis * ((float(x) - fee(float(x)) if x is not None else (1.0 if won else 0.0)) - basis)
    rows.append({"ts": float(d["ts"]), "ask": a, "edge": edge, "room": edge / (1.0 - basis), "won": won, "held": (1.0 if won else 0.0) / basis - 1.0, "traded": traded, "stake": stake})


def line(name, rs):
    if len(rs) < 5:
        print(f"  {name:44s} n {len(rs):3d}")
        return
    n = len(rs)
    print(f"  {name:44s} n {n:3d} | won {sum(1 for r in rs if r['won']) / n:4.0%} | ask {sum(r['ask'] for r in rs) / n:.3f} | held net per $ {sum(r['held'] for r in rs) / n * 100:+6.1f}% | as traded ${sum(r['traded'] for r in rs):+8.2f} on ${sum(r['stake'] for r in rs):.0f}")


hm = lambda t: datetime.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
print(f"window: live crypto_multiscale bets from {hm(SINCE)} ET (the edge screen switched off) to now; {len(rows)} settled bets with the ladder's chance recorded")
print("\n1. what the screen would have done (pass = the ladder's edge is at least 0.10 of the room left)")
line("all bets", rows)
line("the screen would have passed", [r for r in rows if r["room"] >= ROOM])
line("the screen would have dropped", [r for r in rows if r["room"] < ROOM])
print("\n2. by the ladder's edge (its chance less the ask and fee)")
for lo, hi, name in ((-9, -0.10, "edge under -0.10"), (-0.10, -0.05, "-0.10 to -0.05"), (-0.05, 0.0, "-0.05 to 0"), (0.0, 0.05, "0 to +0.05"), (0.05, 9, "+0.05 and over")):
    line(name, [r for r in rows if lo <= r["edge"] < hi])
print("\n3. by six-hour block: passed against dropped")
blocks = defaultdict(list)
for r in rows:
    d = datetime.datetime.fromtimestamp(r["ts"], ET)
    blocks[(d.strftime("%m-%d"), d.hour // 6)].append(r)
for (day, b), rs in sorted(blocks.items()):
    p, q = [r for r in rs if r["room"] >= ROOM], [r for r in rs if r["room"] < ROOM]
    f = lambda xs: f"n {len(xs):2d} held {sum(r['held'] for r in xs) / len(xs) * 100:+6.1f}% traded ${sum(r['traded'] for r in xs):+7.2f}" if xs else "n  0"  # noqa: E731
    print(f"  {day} {b * 6:02d}:00 to {b * 6 + 6:02d}:00 | passed: {f(p)} | dropped: {f(q)}")
print("\n4. does its own recent record say when to trust it? (the 20 bets of the same kind before each bet)")
for kind, pick in (("dropped", lambda r: r["room"] < ROOM), ("passed", lambda r: r["room"] >= ROOM)):
    sub = [r for r in rows if pick(r)]
    up, down = [], []
    for i, r in enumerate(sub):
        if i < 20:
            continue
        prev = sum(x["held"] for x in sub[i - 20:i]) / 20
        (up if prev > 0 else down).append(r)
    line(f"{kind}: after a paying stretch", up)
    line(f"{kind}: after a losing stretch", down)
print("DONE")
