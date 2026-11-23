"""How the slope test measures the rise (2026-11-14, user, on ETH 21:45 YES sold at 0.944 with 127 s left after the bid rose 0.89 -> 0.95 in 40 s: "how do we adjust this so it doesn't see this as .001? that is clearly wrong").
curverisk.still_rising compares the first bid of the look back (8% of the time left) with the last: over 10 s that was 0.950 -> 0.951, a rise of 0.001, though the bid was up 0.03 over 20 s and 0.06 over 43 s. Candidates that only change how the existing gate reads its window: (a) the look back's share of the time left (curve_take_slope_share, live 0.08); (b) the start read as the median of the window's first third instead of its first bid, so one spiking tick at the start no longer sets the rise.
At the second each live profit take sold (the unit the typical minute x sqrt(look / 60) re-read from the quotes, the live noise), which variants would have called the bid still rising (so the take would not have sold then), and what holding paid over the sale for those. Same bets, fee on both legs; the two bets that reversed after their sale (DOGE 17:45, XRP 18:45) show whether a variant would have held them. A held bet might still have sold a moment later (not walked). Chosen after seeing ETH 21:45; said to be so.
Run on the host: python scripts/take_slope_measure.py"""
import bisect
import datetime
import json
import urllib.request

from kalshi_moonshot import config, db, quoteshock
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 18, 0, tzinfo=datetime.UTC).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.execute("SELECT ticker, side, contracts, stake_usd, exit_price, exit_ts, outcome, close_time FROM decisions WHERE lane='crypto_multiscale' AND action='bet' AND stake_usd>0 AND ts>? AND exit_reason LIKE ? ORDER BY exit_ts", (SINCE, "curve risk exit (profit take)%")).fetchall()
VARIANTS = [("live: first bid, 8%", 0.08, False), ("first bid, 12%", 0.12, False), ("first bid, 16%", 0.16, False), ("median start, 8%", 0.08, True), ("median start, 12%", 0.12, True)]


def result_of(ticker, outcome):
    if outcome in ("yes", "no"):
        return outcome
    try:
        with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}", timeout=10) as f:
            r = json.load(f)["market"].get("result")
        return r if r in ("yes", "no") else None
    except Exception:
        return None


tot = {v[0]: [0, 0.0, 0.0, 0, 0, []] for v in VARIANTS}  # held, sale, hold, hold beat sale, sale beat hold, reversal bets held
print("take                    left | rising? " + " | ".join(v[0] for v in VARIANTS) + " | hold minus sale")
for r in rows:
    t = float(r["exit_ts"]); close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp(); left = close - t
    res = result_of(r["ticker"], r["outcome"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (r["ticker"], t - 1000.0, t)).fetchall()
    if res is None or len(q) < 30:
        continue
    side = r["side"]; ts = [float(x[0]) for x in q]
    bd = [(float(x[1]) if side == "yes" else 1.0 - float(x[2])) for x in q]
    mids = [(float(x[0]), (float(x[1]) + float(x[2])) / 2.0) for x in q]
    unit = quoteshock.typical_minute(mids, t, 900.0)
    if unit is None:
        continue
    at = lambda x: bd[max(bisect.bisect_right(ts, x) - 1, 0)]
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    sale = n * (float(r["exit_price"]) - fee_per_contract(float(r["exit_price"]))) - cost
    hold = (n if res == side else 0.0) - cost
    calls = []
    for name, share, med in VARIANTS:
        look = share * left; k = max(int(look), 2)
        w = [at(t - look + i * look / k) for i in range(k + 1)]
        w[-1] = float(r["exit_price"])  # the bid the bot decided at: the taped bid at the sale second lags it by a second or two (ETH 21:45: tape 0.951, decided 0.9444)
        third = sorted(w[: max(k // 3, 1) + 1]); start = third[len(third) // 2] if med else w[0]
        noise = unit * (look / 60.0) ** 0.5
        rising = w[-1] > start and max(w) - w[-1] < noise
        calls.append(rising)
        g = tot[name]
        if rising:
            g[0] += 1; g[1] += sale; g[2] += hold; g[3] += hold > sale; g[4] += sale > hold
            if res != side:
                g[5].append(r["ticker"][2:-3])
    print(f"{r['ticker'][2:-3]:22s} {left:5.0f} | " + " | ".join("YES" if c else "no " for c in calls) + f" | {hold - sale:+.2f}  ended {res}")
print()
for name, _, _ in VARIANTS:
    g = tot[name]
    print(f"{name:22s}: holds {g[0]} of the takes; those sales {g[1]:+.2f}, holding {g[2]:+.2f} (holding minus sale {g[2]-g[1]:+.2f}); holding beat the sale {g[3]}, sale beat holding {g[4]}; reversal bets held: {g[5] or 'none'}")
print("DONE")
