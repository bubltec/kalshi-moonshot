"""The profit take's slope test, replayed on every live crypto_multiscale bet since the take went live, moving only variables that already exist (2026-11-14).

Question: SOL 21:15 YES was sold at 0.982 at 21:06:06 with the bid still climbing (0.880 -> 0.982 over the 43 s look back, 0.996 by the close). The test called it "not rising" because the rise, 0.102, was 0.003 under the market's noise over 43 s (typical minute 0.115 over the last 15 minutes x sqrt(43/60)). Which existing setting would have kept it, and what does that setting do to every other bet?
The variables moved, nothing added: the size a rise over the look back must have to count as still rising (live: the whole of the market's noise over the look back; replayed at half, a quarter and none of it), and curve_take_slope_share (the look back as a share of the time left; live 0.08, also 0.12). The take's own tests are unchanged: curverisk.take_profit, still_rising, CURVE_MIN_BID, CURVE_MIN_LEFT, the fee on both legs.
Method: each bet is walked second by second from its buy to the close on the market's taped quotes (the side's bid; NO = 1 - YES ask). The first second the take would fire is the variant's sale. A bet is compared with what really happened to it (its own exit, or settlement), so a variant changes a bet only when its take fires before the real exit. The live setting is replayed too, to show how closely the walk reproduces the real sales. The take only: the stop, the hour view and the other exits are not walked, so a bet that the real run stopped earlier keeps its real result. Settings chosen after seeing the results are said to be so.
Run on the host: python scripts/take_slope_replay.py (SINCE=epoch seconds to change the start; default 2026-11-14 13:00 ET)."""
import bisect
import datetime
import os

from kalshi_moonshot import config, curverisk, db, quoteshock
from kalshi_moonshot.strategy import fee_per_contract

CURVE_MIN_BID, CURVE_MIN_LEFT = 0.05, 8.0
SINCE = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 14, 18, 0, tzinfo=datetime.UTC).timestamp()))  # 13:00 ET: the take went live around then
SHARES = (0.08, 0.12)
SPANS = (900.0,)
RISE_BARS = (1.0, 0.5, 0.25, 0.0)  # how much of the market's noise a rise over the look back has to beat to count as still rising: live is all of it (1.0)
ET = datetime.timezone(datetime.timedelta(hours=-5))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
bets = st.execute(
    "SELECT id, ts, ticker, side, contracts, stake_usd, exit_price, exit_ts, outcome, close_time FROM decisions "
    "WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall()


class Path:
    """A market's taped quotes, read as the watcher read them."""

    def __init__(self, ticker, side, a, b):
        got = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (ticker, a - 1200.0, b)).fetchall()
        self.ts = [float(r[0]) for r in got]
        self.bid = [(float(r[1]) if side == "yes" else 1.0 - float(r[2])) for r in got]
        self.mid = [(float(r[0]), (float(r[1]) + float(r[2])) / 2.0) for r in got]

    def at(self, t):
        i = bisect.bisect_right(self.ts, t) - 1
        return self.bid[i] if i >= 0 else None

    def mids(self, now):
        i = bisect.bisect_right(self.ts, now)
        return self.mid[:i]


def rising(samples, now, seconds, unit, bar):
    """curverisk.still_rising with the rise bar as a share of the noise: 1.0 is the live test, 0.0 counts any rise over the look back (the bid still has to sit within the noise of its high). With no reading of the unit the live plain test is used."""
    if bar == 1.0 or unit is None or unit <= 0:
        return curverisk.still_rising(samples, now, seconds, unit)
    if seconds <= 0:
        return False
    window = [(t, b) for t, b in samples if t >= now - seconds]
    if not window or not samples or samples[0][0] > now - seconds:
        return True
    bid, high, start = window[-1][1], max(b for _, b in window), window[0][1]
    noise = unit * (seconds / 60.0) ** 0.5
    return bid - start > bar * noise and high - bid < noise


def walk(b, path, share, span, bar=1.0):
    """The second the take would fire for this bet under (share, span), as (time, bid) or None."""
    n, entry = float(b["contracts"]), float(b["stake_usd"]) / float(b["contracts"])
    basis = entry + fee_per_contract(entry)
    close = datetime.datetime.fromisoformat(str(b["close_time"]).replace("Z", "+00:00")).timestamp()
    end = float(b["exit_ts"]) if b["exit_ts"] is not None else close - CURVE_MIN_LEFT
    hist, unit, unit_at = [], None, 0.0
    t = float(b["ts"]) + 1.0
    while t < end:
        bid = path.at(t)
        left = close - t
        if bid is None or left <= CURVE_MIN_LEFT:
            t += 1.0
            continue
        look = share * left
        hist.append((t, bid))
        while hist and hist[0][0] < t - max(60.0, 2.0 * look):
            hist.pop(0)
        if t - unit_at >= 5.0:
            unit_at = t
            unit = quoteshock.typical_minute(path.mids(t), t, span)
        net = bid - fee_per_contract(bid)
        gain, room = net - basis, 1.0 - basis
        pnl = (net - basis) / basis if basis > 0 else 0.0
        if pnl > 0 and bid >= CURVE_MIN_BID and curverisk.take_profit(gain, room, left, 900.0, fee_per_contract(bid), unit) and not rising(hist, t, look, unit, bar):
            return t, bid
        t += 1.0
    return None


def real_value(b):
    n, cost = float(b["contracts"]), float(b["stake_usd"])
    if b["exit_price"] is not None:
        x = float(b["exit_price"])
        return n * (x - fee_per_contract(x)) - cost
    if b["outcome"] in ("yes", "no"):
        return (n if b["outcome"] == b["side"] else 0.0) - cost
    return None


rows = []
for b in bets:
    close = datetime.datetime.fromisoformat(str(b["close_time"]).replace("Z", "+00:00")).timestamp()
    path = Path(b["ticker"], b["side"], float(b["ts"]), close)
    if len(path.ts) < 50:
        continue
    rows.append((b, path, real_value(b)))
print(f"live crypto_multiscale bets since {datetime.datetime.fromtimestamp(SINCE, ET):%m-%d %H:%M} ET with taped quotes: {len(rows)}; with a settled or sold result: {sum(1 for r in rows if r[2] is not None)}")


def run(share, span, bar=1.0):
    out = {}
    for b, path, real in rows:
        w = walk(b, path, share, span, bar)
        n, cost = float(b["contracts"]), float(b["stake_usd"])
        if w is None:
            out[b["id"]] = (real, None)
            continue
        t, bid = w
        sale = n * (bid - fee_per_contract(bid)) - cost
        if b["exit_ts"] is not None and float(b["exit_ts"]) <= t:
            out[b["id"]] = (real, None)  # the real exit came first
        else:
            out[b["id"]] = (sale, (t, bid))
    return out


base = run(0.08, 900.0)
fired = {k: v[1] for k, v in base.items() if v[1]}
print("\nlive setting replayed (share 0.08, span 900 s): the walk's take firings against the real exits")
for b, path, real in rows:
    w = base[b["id"]][1]
    actual = datetime.datetime.fromtimestamp(float(b["exit_ts"]), ET).strftime("%H:%M:%S") if b["exit_ts"] is not None else "held"
    sim = datetime.datetime.fromtimestamp(w[0], ET).strftime("%H:%M:%S") + f" @{w[1]:.2f}" if w else "-"
    if w or b["exit_ts"] is not None:
        print(f"  {b['ticker'][2:-3]:22s} {b['side']:3s} real exit {actual} @{(float(b['exit_price']) if b['exit_price'] is not None else 0):.2f}  walk's take {sim}")

print("\nvariant (rise bar as a share of the noise, look back share, span of the typical minute): bets whose take moved, change against the live setting (fee both legs), better / worse, SOL 21:15")
for span in SPANS:
  for bar in RISE_BARS:
    for share in SHARES:
        v = run(share, span, bar)
        moved = [(k, v[k][0] - base[k][0]) for k in v if v[k][0] is not None and base[k][0] is not None and abs(v[k][0] - base[k][0]) > 1e-9]
        net = sum(d for _, d in moved)
        sol = [b for b, _, _ in rows if b["ticker"].startswith("KXSOL15M-26NOV142115")]
        solnote = ""
        if sol:
            w = v[sol[0]["id"]][1]
            solnote = f"sold {datetime.datetime.fromtimestamp(w[0], ET):%H:%M:%S} @{w[1]:.2f}" if w else "not sold by the take"
        print(f"  rise bar {bar:.2f} x noise, share {share:.2f}, span {span:4.0f}s: moved {len(moved):2d}, net {net:+7.2f}, better {sum(1 for _, d in moved if d > 0)}, worse {sum(1 for _, d in moved if d < 0)}; SOL {solnote}")
print("DONE")
