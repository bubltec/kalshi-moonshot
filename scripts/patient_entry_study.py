"""Would waiting a few seconds for a better price have helped? Replays the bot's own 15-minute favorites on the tape.

For each finished bet of the commodity lane priced 60 cents or more, with recorded quotes, compare buying at the ask when
the bot decided with a limit order `imp` below that ask that waits up to W seconds and is skipped if never filled (filled
when the recorded ask reaches the limit). Reports the return on the fills only: skipped bets are the cost of waiting.
Nothing is traded.    python scripts/patient_entry_study.py tape.db moonshot.db
"""
import bisect, sqlite3, sys
from kalshi_moonshot.strategy import fee_per_contract as fee

t = sqlite3.connect(sys.argv[1]); m = sqlite3.connect(sys.argv[2]); m.row_factory = sqlite3.Row
rows = m.execute("""select ts, ticker, side, ask, outcome from decisions where action='bet' and lane='commodity'
                    and ticker like 'KX%15M%' and ask>=0.6 and outcome in ('yes','no') order by ts""").fetchall()
cache = {}
def quotes(tk):
    if tk not in cache:
        r = t.execute("select ts,bid,ask from quotes where ticker=? order by ts", (tk,)).fetchall()
        cache[tk] = ([x[0] for x in r], r)
    return cache[tk]
data = []
for r in rows:
    ts, rr = quotes(r["ticker"])
    i0 = bisect.bisect_right(ts, r["ts"]) - 1
    if i0 < 0 or r["ts"] - ts[i0] > 60:
        continue
    px = lambda x: x[2] if r["side"] == "yes" else (None if x[1] is None else 1 - x[1])
    base = px(rr[i0])
    if base is not None:
        data.append((r, base, [(x[0] - r["ts"], px(x)) for x in rr[i0 + 1:] if px(x) is not None and x[0] - r["ts"] <= 60]))
print(f"{len(data)} bets with quotes\n{'policy':26} {'placed':>6} {'filled':>6} {'ROI on fills':>13}")
for imp, wait in ((0, 0), (0.01, 10), (0.01, 20), (0.02, 10), (0.02, 20), (0.03, 20)):
    cost = won = 0.0; got = 0
    for r, base, path in data:
        fill = base if imp == 0 else next((base - imp for dt, p in path if dt <= wait and p <= base - imp + 1e-9), None)
        if fill is None:
            continue
        got += 1; cost += fill + fee(fill); won += r["outcome"] == r["side"]
    print(f"{'at the ask' if imp == 0 else f'ask-{int(imp * 100)}c, wait {wait}s':26} {len(data):>6} {got:>6} {(won / cost - 1) if cost else float('nan'):>+13.1%}")
