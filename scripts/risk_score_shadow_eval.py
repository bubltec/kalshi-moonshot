"""crypto_risk_score and its shadow readings, scored on the same bets (rule 4), fee on every leg.
1. The state-record shadow verdict (note text "shadow: would buy / would not buy", bets since 2026-11-19 18:02 ET): held and as sold, by verdict.
2. The shape_timed shadow rows (data/risk_shadow.jsonl, kind shape_timed): a sale at the row's bid against holding to the result, by the bet's look-back shape and by whether the row sold.
Run: scripts/cloud.sh run scripts/risk_score_shadow_eval.py   (SINCE=<epoch> to change the start; default the 11-18 18:31 ET push)"""
import collections, json, os, re
from kalshi_moonshot import config, db
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.strategy import fee_per_contract as fee
s = config.reload()
c = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
st = db.connect(s.db_path, "main", shared=True)
SINCE = float(os.environ.get("SINCE", 1795044660))  # 2026-11-18 18:31 ET
res = {}
def result(tk):
    if tk not in res:
        try: m = c.market(tk); m = m.get("market", m); res[tk] = m.get("result") or ""
        except Exception: res[tk] = ""
    return res[tk]
def held_net(ask, win, k): return k * ((1.0 if win else 0.0) - ask - fee(ask))
def show(title, g):
    print(f"--- {title}")
    for b in sorted(g):
        a = g[b]
        if not a[0]: continue
        print(f"{b:34s} n {a[0]:4d} stake {a[4]:7.0f} | held {a[1]:+8.2f} ({100*a[1]/a[4]:+6.1f}%) | as sold {a[2]:+8.2f} ({100*a[2]/a[4]:+6.1f}%) | win {a[3]/a[0]:.0%}")
rows = st.execute("SELECT ticker, side, ask, stake_usd, ts, exit_price, reason FROM decisions WHERE lane='crypto_risk_score' AND stake_usd IS NOT NULL AND ts > ? ORDER BY ts", (SINCE,)).fetchall()
g = collections.defaultdict(lambda: [0, 0.0, 0.0, 0, 0.0])
for r in rows:
    m = re.search(r"state_record_shadow yes \(shadow: (would (?:not )?buy)", r["reason"] or "")
    if not m or result(r["ticker"]) not in ("yes", "no"): continue
    ask, stake = float(r["ask"]), float(r["stake_usd"]); k = stake / ask; win = result(r["ticker"]) == r["side"]
    h = held_net(ask, win, k)
    sold = h if r["exit_price"] is None else k * (float(r["exit_price"]) - ask - fee(ask) - fee(float(r["exit_price"])))
    a = g[m.group(1)]; a[0] += 1; a[1] += h; a[2] += sold; a[3] += win; a[4] += stake
show("1. state-record shadow verdict, on the bets that carry it", g)
sp = os.path.join(s.data_dir, "risk_shadow.jsonl")
g2 = collections.defaultdict(lambda: [0, 0.0, 0.0, 0, 0.0]); seen = set()
for x in map(json.loads, open(sp)):
    if x.get("kind") != "shape_timed" or x.get("lane") != "crypto_risk_score" or x["ts"] < SINCE: continue
    key = (x["ticker"], x["side"])
    if key in seen or result(x["ticker"]) not in ("yes", "no"): continue
    seen.add(key)
    entry, bid = float(x["entry"]), float(x["bid"]); win = result(x["ticker"]) == x["side"]
    k = 5.0 / entry
    h = held_net(entry, win, k); sale = k * (bid - entry - fee(entry) - fee(bid))
    for name in (f"shape {x['buy']['shape']} / sold={x.get('sold')}", "all shape_timed rows"):
        a = g2[name]; a[0] += 1; a[1] += h; a[2] += sale; a[3] += win; a[4] += 5.0
print("(2: 'held' = holding to the result, 'as sold' = selling at the row's bid, on $5 a bet)")
show("2. shape_timed rows", g2)
