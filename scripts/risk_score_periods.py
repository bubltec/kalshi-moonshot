"""crypto_risk_score bets scored held-to-settlement vs as sold, by period (ET) and price band, fee on every leg.
Question: since the ladder came off (2026-11-18 18:31 ET), which period or band of the lane pays, on the same bets.
Run: scripts/cloud.sh run scripts/risk_score_periods.py"""
import datetime, collections
from kalshi_moonshot import config, db
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.strategy import fee_per_contract as fee
s = config.reload()
c = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
st = db.connect(s.db_path, "main", shared=True)
et = datetime.timezone(datetime.timedelta(hours=-5))
def t(m, d, h, mi): return datetime.datetime(2026, m, d, h, mi, tzinfo=et).timestamp()
CUTS = [(t(11,18,18,31),"18 ladder off      "),(t(11,19,9,8),"19 exit aligned 09:08"),(t(11,19,11,24),"19 shape sale 11:24 "),
        (t(11,19,12,1),"19 cooldown 12:01   "),(t(11,19,15,16),"19 flat-sale off 15:16")]
rows = st.execute("SELECT ticker, side, ask, stake_usd, ts, exit_price FROM decisions WHERE lane='crypto_risk_score' AND stake_usd IS NOT NULL AND ts > ? ORDER BY ts", (CUTS[0][0],)).fetchall()
res = {}
def era(ts):
    name = CUTS[0][1]
    for cut, n in CUTS:
        if ts >= cut: name = n
    return name
def band(a): return "<0.70" if a < 0.70 else ("0.70-0.78" if a < 0.78 else ("0.78-0.85" if a < 0.85 else ">=0.85"))
for key in ("era", "band"):
    g = collections.defaultdict(lambda: [0, 0.0, 0.0, 0, 0.0, 0, 0.0])
    for r in rows:
        tk = r["ticker"]
        if tk not in res:
            try: m = c.market(tk); m = m.get("market", m); res[tk] = m.get("result") or ""
            except Exception: res[tk] = ""
        if res[tk] not in ("yes", "no"): continue
        ask, stake = float(r["ask"]), float(r["stake_usd"]); k = stake / ask; win = res[tk] == r["side"]
        held = k * ((1.0 if win else 0.0) - ask - fee(ask))
        sold_early = r["exit_price"] is not None
        sold = held if not sold_early else k * (float(r["exit_price"]) - ask - fee(ask) - fee(float(r["exit_price"])))
        a = g[era(float(r["ts"])) if key == "era" else band(ask)]
        a[0] += 1; a[1] += held; a[2] += sold; a[3] += win; a[4] += stake
        if sold_early: a[5] += 1; a[6] += sold - held
    print(f"--- by {key}")
    for b in sorted(g):
        a = g[b]
        print(f"{b:24s} n {a[0]:3d} stake {a[4]:6.0f} | held {a[1]:+7.2f} ({100*a[1]/a[4]:+.1f}%) | as sold {a[2]:+7.2f} ({100*a[2]/a[4]:+.1f}%) | win {a[3]/a[0]:.0%} | early sales {a[5]} worth {a[6]:+.2f} vs holding")
