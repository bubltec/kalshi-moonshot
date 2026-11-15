"""A flat bid is fine to hold in the last three minutes; sell only when it starts to dip (2026-11-14, user: "a flat slope under 3 mins is perfectly fine, unless it starts to dip while profitable we should not sell"; "there was nothing indicating that we needed to exit").
Today the take sells once the bid is no longer rising over the look back (flat or falling). The rule: with a fifth of the market or less left (180 s of 900, the user's three minutes), the bid counts as holding while it sits within the market's noise of its look-back high; it sells only when it has fallen further than that from the high. Above that time left the test is the live one (any rise over the look back, within the noise of the high).
Same bets: every live crypto_multiscale profit take since the take went live that sold with 180 s or less left. From the real sale second the take is re-walked second by second on the taped quotes (the side's bid; the typical minute re-read every 5 s): the first second it fires under the rule is the variant's sale, else the bet settles; fee on both legs; compared with what really happened. Take bar, late cap, fee test unchanged. Chosen after seeing the ETH 21:45 sale; said to be so.
Run on the host: python scripts/take_flat_replay.py"""
import bisect
import datetime
import json
import urllib.request

from kalshi_moonshot import config, curvevalue, curverisk, db, quoteshock, variants
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 18, 0, tzinfo=datetime.UTC).timestamp()
import os
FLAT_LEFT = float(os.environ.get("FLAT_LEFT", 900.0 / 5.0))  # 180 s: the user's three minutes; FLAT_LEFT=900 applies the rule at every time left
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.execute("SELECT ticker, side, contracts, stake_usd, exit_price, exit_ts, outcome, close_time FROM decisions WHERE lane='crypto_multiscale' AND action='bet' AND stake_usd>0 AND ts>? AND exit_reason LIKE ? ORDER BY exit_ts", (SINCE, "curve risk exit (profit take)%")).fetchall()


def result_of(ticker, outcome):
    if outcome in ("yes", "no"):
        return outcome
    try:
        with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}", timeout=10) as f:
            r = json.load(f)["market"].get("result")
        return r if r in ("yes", "no") else None
    except Exception:
        return None


MODE = os.environ.get("RISE_RULE", "cut")  # "cut": flat holds when FLAT_LEFT s or less is left; "cushion": the same, but only while curverisk.flat_holds (the cushion backs the price); "scaled": the rise a bid needs shrinks with the share of the market gone (a flat bid holds late, a dip sells), no cut-off


def flat_holds(net_bid, chance, unit, left):
    """curverisk.flat_holds, restated so the replay runs against a host image that does not have it yet."""
    return chance is not None and unit is not None and unit > 0 and left > 0 and net_bid - chance <= unit * (left / 60.0) ** 0.5


def strike_of(ticker):
    try:
        with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}", timeout=10) as f:
            return float(json.load(f)["market"]["floor_strike"])
    except Exception:
        return None


def holds(samples, now, seconds, unit, flat_ok, left=None):
    """Whether the bid is holding: the live test (any rise, within the noise of its high), and with flat_ok also a flat bid, as long as it sits within the noise of its high."""
    if MODE == "scaled" and unit is not None and unit > 0 and left is not None:
        window = [(t, b) for t, b in samples if t >= now - seconds]
        if not window or samples[0][0] > now - seconds:
            return True
        noise = unit * (seconds / 60.0) ** 0.5
        used = min(max(1.0 - left / 900.0, 0.0), 1.0)
        return window[-1][1] - window[0][1] > -used * noise and max(b for _, b in window) - window[-1][1] < noise
    if not flat_ok or unit is None or unit <= 0:
        return curverisk.still_rising(samples, now, seconds, unit)
    window = [(t, b) for t, b in samples if t >= now - seconds]
    if not window or samples[0][0] > now - seconds:
        return True
    return max(b for _, b in window) - window[-1][1] < unit * (seconds / 60.0) ** 0.5


tot = {"rule": [0, 0.0, 0.0, 0, 0]}
print("takes sold with FLAT_LEFT s or less left (default 180): real sale vs the rule's sale (time left, price), and how the market ended")
for r in rows:
    close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp(); t0 = float(r["exit_ts"])
    if close - t0 > FLAT_LEFT:
        continue
    res = result_of(r["ticker"], r["outcome"])
    if res is None:
        continue
    side, n, cost = r["side"], float(r["contracts"]), float(r["stake_usd"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (r["ticker"], t0 - 1300.0, close)).fetchall()
    ts = [float(x[0]) for x in q]; bd = [(float(x[1]) if side == "yes" else 1.0 - float(x[2])) for x in q]
    mids = [(float(x[0]), (float(x[1]) + float(x[2])) / 2.0) for x in q]
    if len(ts) < 50:
        continue
    at = lambda x: bd[max(bisect.bisect_right(ts, x) - 1, 0)]
    real = n * (float(r["exit_price"]) - fee_per_contract(float(r["exit_price"]))) - cost
    entry = cost / n; basis = entry + fee_per_contract(entry)
    idx = variants.spot_index(r["ticker"].split("-")[0], "")
    strike = strike_of(r["ticker"]) if MODE == "cushion" else None
    its, ivs = [], []
    if MODE == "cushion" and idx:
        ig = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (idx, t0 - 1300.0, close)).fetchall()
        its, ivs = [float(x[0]) for x in ig], [float(x[1]) for x in ig]
    hist, unit, unit_at, got, t = [], None, 0.0, None, t0 - 60.0
    while t < close - 8.0:
        bid, left = at(t), close - t; look = 0.08 * left
        hist.append((t, bid))
        while hist and hist[0][0] < t - max(60.0, 2 * look):
            hist.pop(0)
        if t - unit_at >= 5.0:
            unit_at = t; unit = quoteshock.typical_minute(mids[: bisect.bisect_right(ts, t)], t, 900.0)
        if t >= t0 - 1.0:
            net = bid - fee_per_contract(bid); gain, room = net - basis, 1.0 - basis
            ok = left <= FLAT_LEFT
            if MODE == "cushion" and ok and gain > 0 and curverisk.take_profit(gain, room, left, 900.0, fee_per_contract(bid), unit):
                if idx is not None and strike is not None:
                    jt = bisect.bisect_right(its, t)
                    rd = curvevalue.read(its[max(jt - 1100, 0):jt], ivs[max(jt - 1100, 0):jt], t, strike, side, left, "900+180") if jt else None
                else:
                    rd = None
                ok = flat_holds(net, rd.p if rd else None, unit, left)
            if gain > 0 and bid >= 0.05 and curverisk.take_profit(gain, room, left, 900.0, fee_per_contract(bid), unit) and not holds(hist, t, look, unit, ok, left):
                got = (t, bid); break
        t += 1.0
    if got:
        v = n * (got[1] - fee_per_contract(got[1])) - cost; how = f"{close - got[0]:.0f}s @{got[1]:.3f}"
    else:
        v = (n if res == side else 0.0) - cost; how = "settles"
    k = tot["rule"]; k[0] += 1; k[1] += real; k[2] += v; k[3] += v > real + 1e-9; k[4] += v < real - 1e-9
    print(f"  {r['ticker'][2:-3]:22s} {side:3s} real {close - t0:4.0f}s left @{float(r['exit_price']):.3f}, ended {res}: rule -> {how} ({v - real:+.2f})")
k = tot["rule"]
print(f"\n{k[0]} bets: real {k[1]:+.2f}, rule {k[2]:+.2f}, change {k[2] - k[1]:+.2f}; better {k[3]}, worse {k[4]}")
print("DONE")
