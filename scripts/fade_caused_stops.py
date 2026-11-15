"""Since the hour view's fade went live (2026-11-15 09:36 ET), which live stops did the fade itself cause, and how did those bets end?

Same bets, one question (CLAUDE.md rule 4): every live crypto_multiscale sale by the max-loss stop or the line since the fade went live. For each, the hour view's chance at the sale (curverisk.long_chance from the Kalshi perp exactly as watcher._risk_long reads it), the bar under the old fixed hold (curve_risk_confirm, 0.4) and the bar under the fade (curverisk.confirm_bar), and whether the fade made the difference: the hour view was at or over the old bar (the old rule would have held) but under the faded one. The bet's own result, and what holding to settlement would have paid. Rule 6: a rule whose score turns negative over its window is flagged.

Run on the host: python scripts/fade_caused_stops.py (SINCE=epoch seconds to change the start; default 2026-11-15 09:36 ET).
"""
import bisect
import datetime
import json
import os
import urllib.request

from kalshi_moonshot import config, curverisk, db
from kalshi_moonshot.strategy import fee_per_contract
from kalshi_moonshot.watcher import perp_index

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 15, 9, 36, tzinfo=ET).timestamp()))
CONFIRM = 0.4
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.execute("SELECT ts, ticker, side, contracts, stake_usd, exit_price, exit_ts, exit_reason, close_time FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND exit_ts IS NOT NULL AND exit_reason LIKE 'curve risk exit%' AND exit_reason NOT LIKE '%profit take%' AND exit_ts>? ORDER BY exit_ts", (SINCE,)).fetchall()
f = lambda t: datetime.datetime.fromtimestamp(float(t), ET).strftime("%H:%M:%S")
caused = []
print(f"live stops and line sales since {f(SINCE)}: {len(rows)}")
for r in rows:
    close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp(); t = float(r["exit_ts"]); left = close - t
    kp = perp_index(r["ticker"].split("-")[0])
    g = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (kp, close - 900 - 3800, close)).fetchall() if kp else []
    ts, vs = [float(x[0]) for x in g], [float(x[1]) for x in g]
    i = bisect.bisect_left(ts, close - 900.0); ref = vs[i] if i < len(vs) and ts[i] - (close - 900.0) <= 60 else None
    k = bisect.bisect_right(ts, t); a = bisect.bisect_left(ts, t - 3700)
    lc = curverisk.long_chance(ts[a:k], vs[a:k], t, ref or 0.0, r["side"], left) if ref and k - a > 10 else None
    bar_fade = curverisk.confirm_bar(CONFIRM, left)
    try:
        res = json.load(urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{r['ticker']}", timeout=10))["market"].get("result")
    except Exception:
        res = None
    n, cost = float(r["contracts"]), float(r["stake_usd"]); x = float(r["exit_price"])
    sold = n * (x - fee_per_contract(x)) - cost
    held = None if res not in ("yes", "no") else (n if res == r["side"] else 0.0) - cost
    fade = lc is not None and lc >= CONFIRM and lc < bar_fade
    if fade:
        caused.append((sold, held))
    print(f"  {r['ticker'][2:-3]:20s} {r['side']:3s} sold {f(t)} {left:4.0f}s left @{x:.2f}: hour view {lc if lc is None else round(lc, 3)}, old bar {CONFIRM}, faded bar {bar_fade:.3f}{'  <- the fade released it' if fade else ''}; result {sold:+.2f}, holding {held if held is None else round(held, 2)} (market {res})")
print(f"\nstops the fade caused: {len(caused)}; their sale result {sum(a for a, _ in caused):+.2f}, holding would have paid {sum(b for _, b in caused if b is not None):+.2f}")
print("DONE")
