"""The curve exit against the risk exit, on the bot's own live bets (2026-11-16, user: "i know this was working... so what happened where it got removed??"; "Yes, compare them").

Two stretches of live 15-minute bets, from the decisions and windows tables only:
  curve exit   bets placed 2026-11-10 00:00 ET to 2026-11-13 09:45 ET (the curve exit stack live, the risk exit in shadow or absent)
  risk exit    bets placed 2026-11-13 11:02 ET to 2026-11-16 18:36 ET (the risk exit the only exit, until its max-loss stop was switched off)
The buying changed between the two stretches too, so the totals are not a fair comparison of the exits. The fair one is each sale against HOLDING THE SAME BET to settlement (fee on every leg): that isolates what the exit added or cost, whatever was bought. By the kind of sale as its recorded reason names it, for the crypto lanes and the commodity lanes apart.

Run on the host: scripts/cloud.sh run scripts/exit_eras_compare.py
"""
import datetime
import re
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
at = lambda *a: datetime.datetime(2026, 11, *a, tzinfo=ET).timestamp()  # noqa: E731
ERAS = (("curve exit (11-10 to 11-13 09:45)", at(10, 0, 0), at(13, 9, 45)), ("risk exit (11-13 11:02 to 11-16 18:36)", at(13, 11, 2), at(16, 18, 36)))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
res = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (ERAS[0][1],)).fetchall()}


def kind(reason):
    r = str(reason or "").lower()
    m = re.search(r"curve risk exit \(([a-z ]+)\)", r)
    if m:
        return "risk exit: " + m.group(1)
    if r.startswith("curve risk exit"):
        return "risk exit: the 0.50 line"
    for key, name in (("time-to-go", "curve exit: time-to-go"), ("time to go", "curve exit: time-to-go"), ("trajectory", "curve exit: trajectory"), ("market disagrees", "curve exit: gap"), ("gap", "curve exit: gap"), ("curve exit", "curve exit: margin sale"), ("curve sale", "curve exit: margin sale")):
        if key in r:
            return name
    return "other: " + re.sub(r"[\d.]+", "#", r)[:34]


for era, lo, hi in ERAS:
    rows = []
    for d in st.execute("SELECT ts, lane, ticker, side, ask, stake_usd, exit_price, exit_ts, exit_reason FROM decisions WHERE mode='trade' AND action='bet' AND stake_usd>0 AND ts>=? AND ts<? ORDER BY ts", (lo, hi)).fetchall():
        if d["ticker"] not in res or not d["ask"] or "15M" not in d["ticker"]:
            continue
        result, close = res[d["ticker"]]
        a = float(d["ask"])
        basis = a + fee(a)
        if not (0.03 < a < 0.99) or basis >= 1:
            continue
        n = float(d["stake_usd"]) / basis
        won = result == d["side"]
        hold = ((1.0 if won else 0.0) - basis) * n
        sold = d["exit_price"] is not None and d["exit_ts"] is not None and float(d["exit_ts"]) < close - 1
        real = ((float(d["exit_price"]) - fee(float(d["exit_price"]))) - basis) * n if sold else hold
        rows.append(("crypto" if d["lane"].startswith("crypto") else "commodity", kind(d["exit_reason"]) if sold else "held to settlement", real, hold, won, float(d["stake_usd"])))
    print(f"\n=== {era}")
    for grp in ("crypto", "commodity"):
        sub = [r for r in rows if r[0] == grp]
        if not sub:
            continue
        stake = sum(r[5] for r in sub)
        print(f"  {grp} lanes: {len(sub)} bets, ${stake:.0f} staked | as traded ${sum(r[2] for r in sub):+.2f} ({sum(r[2] for r in sub) / stake * 100:+.1f}%) | if every bet were held ${sum(r[3] for r in sub):+.2f} ({sum(r[3] for r in sub) / stake * 100:+.1f}%) | the exits added ${sum(r[2] - r[3] for r in sub):+.2f}")
        kinds = defaultdict(list)
        for r in sub:
            kinds[r[1]].append(r)
        for k, rs in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
            if k == "held to settlement":
                print(f"      {k:36s} n {len(rs):4d} | won {sum(1 for r in rs if r[4]) / len(rs):4.0%}")
            else:
                print(f"      {k:36s} n {len(rs):4d} | would have won held {sum(1 for r in rs if r[4]) / len(rs):4.0%} | sale less hold ${sum(r[2] - r[3] for r in rs):+8.2f} (per sale ${sum(r[2] - r[3] for r in rs) / len(rs):+.2f})")
print("DONE")
