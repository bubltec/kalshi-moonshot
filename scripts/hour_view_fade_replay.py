"""Fade the hour view's hold as the close nears (2026-11-15, user: "Yes replay it so it does the fade", after ETH 09:00 YES was held to a full loss: the stop wanted to sell from 104 s left, the hour view (curve_risk_confirm 0.4, curverisk.long_chance from the Kalshi perp's last hour) stayed at 0.44 to 0.61 until 37 s left, then the bid was under the 0.05 minimum).
Live: a risk sale (the max-loss stop, the line sale) is held whenever the hour view's chance that the side pays is at or over curve_risk_confirm (0.4). Candidates change only that hold: (a) live; (b) the bar the hour view must reach to hold rises from 0.4 to 1.0 with the squeeze clock (curverisk.squeeze_clock: nothing in the first half of the market, all of it at the close); (c) the same rise, but only in the last fifth of the market (0.4 at 180 s left to 1.0 at the close); (d) no hour view at all (the bound).
Same bets: every live crypto_multiscale bet since the take went live (13:00 ET 2026-11-14), each walked second by second from its buy on the taped quotes (the side's bid; the typical minute re-read every 5 s; the spot chance from the settlement index with the lane's own read; the hour view from the Kalshi perp exactly as watcher._risk_long reads it), with the live take (clock bar and late cap, any rise over 8% of the time left within the noise of its high, the flat hold, the 0.02 cushion allowance, HIGH_BID 0.95) and the live risk sale: the hour-view hold, then the max-loss stop (loss at or over 0.5 x the chance, bid at or over 0.05) or the line sale (chance under 0.5 and a loss), each after its wait (stop_wait with curve_risk_stop_hold 0.5, wait with curve_wait_share 0.25, curve_wait_default 15 s). A bet nothing sells settles; fee on both legs. Said plainly: an approximation (its live baseline is printed against the real result); the squeeze nudge and the hour history factor (0 on this lane's quote path) are not walked; one night; not split by curve shape; the fade shapes were chosen after seeing ETH 09:00.
Run on the host: python scripts/hour_view_fade_replay.py"""
import bisect
import datetime
import json
import urllib.request

from kalshi_moonshot import config, curvevalue, curverisk, db, quoteshock, variants
from kalshi_moonshot.strategy import fee_per_contract
from kalshi_moonshot.watcher import perp_index

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 13, 0, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.execute("SELECT ts, ticker, side, contracts, stake_usd, exit_price, exit_ts, outcome, close_time FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall()
mk = {}


def market(ticker):
    if ticker not in mk:
        try:
            with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}", timeout=10) as f:
                m = json.load(f)["market"]
            mk[ticker] = (m.get("result") if m.get("result") in ("yes", "no") else None, float(m["floor_strike"]) if m.get("floor_strike") else None)
        except Exception:
            mk[ticker] = (None, None)
    return mk[ticker]


def flat_holds(net, chance, unit, left):
    return chance is not None and unit is not None and unit > 0 and left > 0 and net - chance <= unit * (left / 60.0) ** 0.5


def ticks(index_id, a, b):
    if not index_id:
        return [], []
    g = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (index_id, a, b)).fetchall()
    return [float(x[0]) for x in g], [float(x[1]) for x in g]


data = []
for r in rows:
    close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp(); t0 = float(r["ts"])
    res, strike = market(r["ticker"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (r["ticker"], t0 - 1000.0, close)).fetchall()
    if res is None or strike is None or len(q) < 30:
        continue
    series = r["ticker"].split("-")[0]; side = r["side"]; n, cost = float(r["contracts"]), float(r["stake_usd"])
    ts = [float(x[0]) for x in q]; bd = [(float(x[1]) if side == "yes" else 1.0 - float(x[2])) for x in q]
    mids = [(float(x[0]), (float(x[1]) + float(x[2])) / 2.0) for x in q]
    its, ivs = ticks(variants.spot_index(series, ""), t0 - 1300.0, close)
    kp = perp_index(series)
    kts, kvs = ticks(kp, t0 - 3800.0, close)
    ref = None
    if kts:
        j = bisect.bisect_left(kts, close - 900.0)
        if j < len(kts) and kts[j] - (close - 900.0) <= 60.0:
            ref = kvs[j]
    real = (n * (float(r["exit_price"]) - fee_per_contract(float(r["exit_price"]))) - cost) if r["exit_price"] is not None else ((n if res == side else 0.0) - cost)
    data.append(dict(t=r["ticker"], side=side, n=n, cost=cost, close=close, t0=t0, res=res, strike=strike, ts=ts, bd=bd, mids=mids, its=its, ivs=ivs, kts=kts, kvs=kvs, ref=ref, real=real, chance={}, long={}))


def chance_at(d, t, left):
    k = int(t)
    if k not in d["chance"]:
        jt = bisect.bisect_right(d["its"], t)
        rd = curvevalue.read(d["its"][max(jt - 1100, 0):jt], d["ivs"][max(jt - 1100, 0):jt], t, d["strike"], d["side"], left, "900+180") if jt else None
        d["chance"][k] = rd.p if rd else None
    return d["chance"][k]


def long_at(d, t, left):
    k = int(t // 5)
    if k not in d["long"]:
        v = None
        if d["kts"] and d["ref"]:
            a = bisect.bisect_left(d["kts"], t - 3700.0); b = bisect.bisect_right(d["kts"], t)
            v = curverisk.long_chance(d["kts"][a:b], d["kvs"][a:b], t, d["ref"], d["side"], left)
        if v is None and d["its"]:
            a = bisect.bisect_left(d["its"], t - 3700.0); b = bisect.bisect_right(d["its"], t)
            v = curverisk.long_chance(d["its"][a:b], d["ivs"][a:b], t, d["strike"], d["side"], left)
        d["long"][k] = v
    return d["long"][k]


CONFIRM = 0.4


def eff_confirm(variant, left):
    if variant == "live":
        return CONFIRM
    if variant == "squeeze clock":
        return CONFIRM + (1.0 - CONFIRM) * curverisk.squeeze_clock(left, 900.0)
    if variant == "last fifth":
        return CONFIRM + (1.0 - CONFIRM) * min(max(1.0 - left / 180.0, 0.0), 1.0)
    return 9.0  # no hour view


def walk(d, variant):
    entry = d["cost"] / d["n"]; basis = entry + fee_per_contract(entry); room = 1.0 - basis
    at = lambda x: d["bd"][max(bisect.bisect_right(d["ts"], x) - 1, 0)]
    hist, unit, unit_at, since, t = [], None, 0.0, 0.0, d["t0"] + 1.0
    while t < d["close"] - 8.0:
        bid, left = at(t), d["close"] - t; look = 0.08 * left
        hist.append((t, bid))
        while hist and hist[0][0] < t - max(60.0, 2 * look):
            hist.pop(0)
        if t - unit_at >= 5.0:
            unit_at = t; unit = quoteshock.typical_minute(d["mids"][: bisect.bisect_right(d["ts"], t)], t, 900.0)
        net = bid - fee_per_contract(bid); gain = net - basis; pnl = gain / basis
        if gain > 0 and bid >= 0.05 and curverisk.take_profit(gain, room, left, 900.0, fee_per_contract(bid), unit):
            ch = chance_at(d, t, left)
            strict_hold = ch is not None and ch > net - curverisk.CUSHION_NUDGE
            flat_ok = (left <= 180.0 or bid >= curverisk.HIGH_BID) and flat_holds(net, ch, unit, left)
            if not strict_hold and not curverisk.still_rising(hist, t, look, unit, flat_ok):
                return d["n"] * net - d["cost"], "take"
        lg = long_at(d, t, left)
        if lg is not None and lg >= eff_confirm(variant, left):
            since = 0.0; t += 1.0; continue
        q = min(max(net, 0.0), 1.0)
        stop = curverisk.stopped(pnl, q, 0.5) and bid >= 0.05
        if not stop and (bid < 0.05 or not curverisk.sale_ok(q, 0.5, pnl, 0.15)):
            since = 0.0; t += 1.0; continue
        if since <= 0:
            since = t
        wait = curverisk.stop_wait(q, 0.5, left, 15.0, 0.5) if stop else curverisk.wait(q, 0.5, left, 15.0, 0.25)
        if t - since >= wait:
            return d["n"] * net - d["cost"], "stop" if stop else "line"
        t += 1.0
    return (d["n"] if d["res"] == d["side"] else 0.0) - d["cost"], "settle"


VARIANTS = ["live", "squeeze clock", "last fifth", "no hour view"]
print(f"live bets since the take went live with taped quotes, a strike and a result: {len(data)}; the real run made {sum(d['real'] for d in data):+.2f}; bets with an hour view reading: {sum(1 for d in data if d['kts'] and d['ref'])}")
res = {}
for v in VARIANTS:
    rows_ = [walk(d, v) for d in data]; res[v] = rows_
    kinds = {k: [r for r in rows_ if r[1] == k] for k in ("take", "stop", "line", "settle")}
    print(f"{v:14s}: total {sum(r[0] for r in rows_):+8.2f} | " + " | ".join(f"{k} {len(g):2d} {sum(r[0] for r in g):+7.2f}" for k, g in kinds.items()))
base = res["live"]
print("\nchange against the live hold, bets that differ by more than $0.50:")
for v in VARIANTS[1:]:
    diffs = [(i, res[v][i][0] - base[i][0]) for i in range(len(data)) if abs(res[v][i][0] - base[i][0]) > 0.5]
    print(f"{v:14s}: net {sum(r[0] for r in res[v]) - sum(b[0] for b in base):+8.2f}; better on {sum(1 for _, x in diffs if x > 0)}, worse on {sum(1 for _, x in diffs if x < 0)}")
    if v in ("squeeze clock", "last fifth"):
        for i, x in sorted(diffs, key=lambda y: y[1]):
            d = data[i]
            print(f"     {d['t'][2:-3]:20s} {d['side']:3s} {datetime.datetime.fromtimestamp(d['t0'], ET):%H:%M} live {base[i][1]} {base[i][0]:+7.2f} -> {res[v][i][1]} {res[v][i][0]:+7.2f} ({x:+.2f}); ended {d['res']}")
print("DONE")
