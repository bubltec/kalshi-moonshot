"""Both legs resting, no fees: buy a 30-48c side with a resting bid, then rest a sell order a few cents above. Replayed from the tape. Places nothing.

Every 30 s from minute 1 to 10 of a 15-minute window, for each side whose ask is 30-48c with a spread of 4c or less:
  buy   a resting bid at the bid. Cautious fill: it fills only if the side's ask comes down to our price within `wait` seconds (a trade through it is not counted). No fee.
  sell  once filled, a resting sell `target` above the price paid. Cautious fill: it fills only if the side's bid reaches that price. No fee.
  if the sell has not filled after `hold` minutes (or 30 s before the close), either (a) sell at the bid (the taker fee on the sale) or (b) hold to the result.
Returns per dollar paid. "per fill" is among bets that got in; "per moment" counts a bid that never filled as zero. Standard errors across windows.
    docker compose exec -T bot python - < scripts/maker_scalp_study.py
"""
import bisect
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M",
          "KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXBRENT15M", "KXCOPPER15M", "KXNATGAS15M", "KXPLATINUM15M", "KXPALLADIUM15M")
data = []
for m in main.db.execute(f"SELECT ticker, series, result, close_ts FROM windows WHERE series IN ({','.join('?' * len(SERIES))}) AND result IN ('yes','no')", SERIES).fetchall():
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"],)).fetchall()
    if len(q) >= 60:
        data.append((m, [r[0] for r in q], q))
print(f"{len(data)} windows with a tape")


def run(target, hold, wait, lo, hi, mode):
    out = []
    for m, qts, q in data:
        close = m["close_ts"]
        for side in ("yes", "no"):
            asks = [r[2] if side == "yes" else 1 - r[1] for r in q]
            bids = [r[1] if side == "yes" else 1 - r[2] for r in q]
            win = 1.0 if m["result"] == side else 0.0
            t = close - 900 + 60
            while t < close - 900 + 600:
                i = bisect.bisect_right(qts, t) - 1
                t += 30
                if i < 0 or not (lo <= asks[i] <= hi) or asks[i] - bids[i] > 0.04 or bids[i] <= 0:
                    continue
                px = bids[i]
                jmax = bisect.bisect_right(qts, min(qts[i] + wait, close - 60))
                f = next((j for j in range(i + 1, jmax) if asks[j] <= px + 1e-9), None)
                if f is None:
                    out.append((m["ticker"], None, False))
                    continue
                end = min(qts[f] + hold * 60, close - 30)
                kmax = bisect.bisect_right(qts, end)
                g = next((k for k in range(f + 1, kmax) if bids[k] >= px + target - 1e-9), None)
                if g is not None:
                    out.append((m["ticker"], target / px, True))
                elif mode == "taker":
                    e = bids[max(f, kmax - 1)]
                    out.append((m["ticker"], (e - fee(e) - px) / px, False))
                else:
                    out.append((m["ticker"], (win - px) / px, False))
    return out


def show(label, rows):
    if not rows:
        return f"{label:44} none"
    per = defaultdict(list)
    for t_, r, _ in rows:
        per[t_].append(0.0 if r is None else r)
    mm = [sum(v) / len(v) for v in per.values()]
    se = 100 * math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) if len(mm) > 2 else float("nan")
    fills = [r for _, r, _ in rows if r is not None]
    hit = [x for _, r, x in rows if r is not None]
    return (f"{label:44} moments {len(rows):5}  bought {len(fills) / len(rows):4.0%}  sell filled {statistics.mean(hit) if hit else 0:4.0%}  per fill "
            f"{statistics.mean(fills) if fills else float('nan'):+6.1%}  per moment {statistics.mean(0.0 if r is None else r for _, r, _ in rows):+6.1%} +-{se:.1f}")


seen = set()
for lo, hi in ((0.30, 0.48),):
    for mode, name in (("taker", "then sell at the bid (taker fee)"), ("hold", "then hold to the result")):
        print(f"\\nask {lo:.2f}-{hi:.2f}, buy waits 120 s, unsold after 4 min: {name}")
        for target in (0.01, 0.02, 0.03, 0.05):
            rows = run(target, 4.0, 120.0, lo, hi, mode)
            line = show(f"  rest the sell {int(target * 100)}c above", rows)
            seen.add(line.split("per fill")[1])
            print(line)
if len(seen) < 3:
    print("WARNING: the variants gave nearly the same answer; check the study before trusting it")
