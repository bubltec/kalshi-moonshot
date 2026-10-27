"""CAN A STOP SELL WITHOUT THE TAKER FEE: every favorites bet on record (15-minute, 60c+, one per market, side and price) is replayed to the moment the live stop would sell (45% under the price paid, after strategy.stop_wait). The live sale is an immediate order at the bid
(it pays the taker fee). The alternative tested here: rest a post-only SELL one cent under the side's ask for W seconds (it fills when a buyer reaches that price, and pays no fee); if it has not filled after W seconds, sell at once at whatever the bid is then (taker fee).
Per contract: proceeds of each way net of fees, and how often the rest filled. A fill is a quote whose side bid reached our price, or a trade by takers buying that side at or above it.
Read-only. Places nothing.
    docker compose exec -T bot python - < scripts/stop_rest_study.py
"""
import bisect
import statistics
from datetime import datetime

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot.strategy import stop_wait

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
STOP, CAP = 0.45, 120.0
WAITS = (2, 5, 10, 20)
rows = main.db.execute(
    """SELECT ts, ticker, side, ask, close_time FROM decisions
       WHERE action='bet' AND ask>=0.60 AND outcome IN ('yes','no') AND ticker LIKE '%15M%' AND COALESCE(lane,'') NOT IN ('manual','burst_flip','settlement_flip','crypto_moonshot','settlement_edge')
       ORDER BY ts""").fetchall()


def family(series):
    if series[2:].startswith(("BTC", "ETH", "SOL", "XRP", "DOGE")):
        return "crypto"
    return "fx" if any(x in series for x in ("EUR", "GBP", "JPY", "AUD", "CAD")) else "commodity"


cases = []
seen = set()
for r in rows:
    key = (r["ticker"], round(float(r["ask"]), 2), r["side"])
    if key in seen:
        continue
    seen.add(key)
    try:
        close_ts = datetime.fromisoformat(str(r["close_time"])).timestamp()
    except (TypeError, ValueError):
        continue
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], r["ts"], close_ts)).fetchall()
    if len(q) < 5:
        continue
    side, entry = r["side"] or "yes", float(r["ask"])
    line = entry * (1.0 - STOP)
    since = None
    trig = None
    for i, (ts, bid, ask) in enumerate(q):
        b = bid if side == "yes" else 1.0 - ask
        if b <= line + 1e-9:
            if since is None:
                since = ts
            if ts - since >= stop_wait(CAP, close_ts - since, True):
                trig = i
                break
        else:
            since = None
    if trig is None:
        continue
    ts0 = q[trig][0]
    cases.append((r["ticker"], side, close_ts, q, trig, family(r["ticker"].split("-")[0])))
print(len(cases), "stops replayed")


def sb(x, side):  # the side's bid and ask from a YES-book quote
    return (x[1], x[2]) if side == "yes" else (1.0 - x[2], 1.0 - x[1])


def run(case, W):
    tk, side, close_ts, q, i, _ = case
    t0 = q[i][0]
    b0, a0 = sb(q[i], side)
    now_net = b0 - fee(b0)
    p = round(a0 - 0.01, 2)
    if p <= b0 + 1e-9:
        return now_net, now_net, False  # no room to rest a sale above the bid
    filled = False
    j = i + 1
    while j < len(q) and q[j][0] <= t0 + W:
        b, _a = sb(q[j], side)
        if b >= p - 1e-9:
            filled = True
            break
        j += 1
    if not filled:
        for fl in tape.execute("SELECT yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts > ? AND ts <= ?", (tk, int(t0), int(t0 + W))).fetchall():
            if fl[2] is None:
                continue
            yes_price = fl[2] if side == "yes" else 1.0 - fl[2]
            if (side == "yes" and fl[0] > 0 and yes_price >= p - 1e-9) or (side == "no" and fl[1] > 0 and yes_price >= p - 1e-9):
                filled = True
                break
    if filled:
        return now_net, p, True
    k = bisect.bisect_right([x[0] for x in q], t0 + W) - 1
    b1, _ = sb(q[max(k, i)], side)
    return now_net, b1 - fee(b1), False


for fam in (None, "crypto", "fx", "commodity"):
    g = [c for c in cases if fam is None or c[5] == fam]
    if len(g) < 20:
        continue
    print(f"\n{fam or 'all'}: {len(g)} stops; sold at the bid now, net of the fee: {statistics.mean(run(c, 2)[0] for c in g):.3f} per contract")
    for W in WAITS:
        res = [run(c, W) for c in g]
        gain = [b - a for a, b, _ in res]
        fills = sum(1 for _, _, f in res if f)
        fell = [b - a for a, b, f in res if not f]
        print(f"  rest {W:2d} s: filled {100 * fills / len(res):3.0f}%, average gain over selling now {100 * statistics.mean(gain):+5.2f}c per contract, filled ones {100 * statistics.mean([b - a for a, b, f in res if f] or [0]):+5.2f}c, the rest (sold later) {100 * statistics.mean(fell or [0]):+5.2f}c")
