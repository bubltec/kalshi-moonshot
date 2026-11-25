"""A first look: which stored buy-time readings differ between death traps and the other PriskP bets? (2026-11-25; user: "we need to figure out how to prevent the buys from buying into a death trap".)
QUESTION (one): on every live priskp bet with a result (since 2026-11-23), is each stored reading (decisions.factors: elapsed_min, spread, imbalance, move_90, range_90, activity_60, prior_15, bid, ask, rise_30, rise_90, dip_90, held_min, updates_60; signed for the bet's side where they have a sign: imbalance and prior_15 are taken as stored) different, by its median, between death traps (net -0.40 a contract or worse after both fees, as the lane ran it) and the other bets?
HONEST LIMITS: this LOOKS at 14 readings at once with about ten traps, so some will differ by chance; it is a list of hypotheses to log forward and test on later days, not a finding. n is small (the lane has run since 2026-11-23); 'as run' mixes the lane's exits with the entry; the stored readings are the ones the bot logged at the buy (priskp.py and runner.py define them). A lead only; no rule on it.
Run on the host: scripts/cloud.sh run scripts/priskp_trap_factors_look.py"""
import functools
import json
import statistics
import traceback
import urllib.request

from kalshi_moonshot import config, db

print = functools.partial(print, flush=True)
TRAP = -0.40
s = config.load()
st = db.connect(s.db_path, "main", shared=True)


def fee(p):
    return 0.07 * p * (1.0 - p)


def market(t):
    with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{t}", timeout=20) as r:
        return json.load(r)["market"]


rows = st.execute("SELECT ticker, side, ts, stake_usd, contracts, exit_ts, exit_price, factors FROM decisions WHERE lane='priskp' AND action='bet' AND mode='trade' ORDER BY ts").fetchall()
traps, rest = [], []
for r in rows:
    try:
        if not r["factors"]:
            continue
        f = json.loads(r["factors"])
        n = float(r["contracts"])
        pay = float(r["stake_usd"]) / n
        cost = pay + fee(pay)
        if r["exit_ts"]:
            net = float(r["exit_price"]) - fee(float(r["exit_price"])) - cost
        else:
            res = market(r["ticker"]).get("result")
            if not res:
                continue
            net = (1.0 if res == r["side"] else 0.0) - cost
        f["_net"] = net
        (traps if net <= TRAP else rest).append(f)
    except Exception:
        print(r["ticker"], "failed", traceback.format_exc()[-200:])
print(f"{len(traps)} death traps (net {TRAP:+.2f} or worse), {len(rest)} other bets")
allf = traps + rest
keys = sorted({k for f in allf for k, v in f.items() if k != "_net" and isinstance(v, (int, float)) and not isinstance(v, bool)})
print(f"{'reading':14s} {'trap median':>12s} {'others median':>14s}   trap range            others' 25-75%")
for k in keys:
    a = [f[k] for f in traps if isinstance(f.get(k), (int, float))]
    b = [f[k] for f in rest if isinstance(f.get(k), (int, float))]
    if len(a) < 3 or len(b) < 10:
        continue
    q = statistics.quantiles(b, n=4)
    print(f"{k:14s} {statistics.median(a):12.3f} {statistics.median(b):14.3f}   {min(a):8.3f}..{max(a):8.3f}   {q[0]:8.3f}..{q[2]:8.3f}")
