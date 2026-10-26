"""Is there value in the e-sport match markets (eSoccer, eBasketball, eFootball)? Replayed from the tape, results read from Kalshi. Places nothing.

A market is a team to win (or the draw). "Live" starts the first time it has 10 quote changes inside two minutes. For every finished market:
  calibration  at 1, 3 and 5 minutes into the live part (book within 6c): how often the side won against its price, and the return of buying at the ask and holding
  dips         every 15 s in the live part, a side priced 25-75c: a 'dip' is a bid 8c or more under its 2-minute high. Buy at the ask, sell at the bid when it is 8c over the
               price paid, else after 3 minutes. Dips against every other moment; fees both ways.
Standard errors are taken across events (a match's markets move together).
    docker compose exec -T bot python - < scripts/etape_value_study.py
"""
import bisect
import math
import statistics
import time
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
client = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
GAMES = (("eSoccer", "KXESOCCERGAME"), ("eBasketball", "KXEBASKETBALLGAME"), ("eFootball", "KXEFOOTBALLGAME"))
now = time.time()


def se(rows, f):
    per = defaultdict(list)
    for r in rows:
        per[r["event"]].append(f(r))
    means = [sum(v) / len(v) for v in per.values()]
    if len(means) < 2:
        return float("nan")
    mm = sum(means) / len(means)
    return math.sqrt(sum((x - mm) ** 2 for x in means) / (len(means) - 1) / len(means)) * 100


def live_start(ts):
    for i in range(len(ts)):
        j = bisect.bisect_left(ts, ts[i] - 120)
        if i - j >= 10:
            return ts[i]
    return None


for name, prefix in GAMES:
    tickers = [r[0] for r in tape.execute("SELECT ticker FROM quotes WHERE ticker LIKE ? GROUP BY ticker HAVING MAX(ts) < ? AND COUNT(*) >= 50", (prefix + "-%", now - 1800)).fetchall()]
    mk = {}
    for tk in tickers:
        try:
            res = client.market(tk).get("result")
        except Exception:
            continue
        if res not in ("yes", "no"):
            continue
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts", (tk,)).fetchall()
        ts = [r[0] for r in q]
        st = live_start(ts)
        if st is None:
            continue
        mk[tk] = {"q": q, "ts": ts, "start": st, "win": res == "yes", "event": tk.rsplit("-", 1)[0]}
    print(f"\n{'=' * 100}\n{name}: {len(mk)} finished markets with a result, {len({m['event'] for m in mk.values()})} matches")
    if not mk:
        continue
    spreads = [r[2] - r[1] for m in mk.values() for r in m["q"] if m["start"] <= r[0] and r[1] > 0]
    dur = [m["ts"][-1] - m["start"] for m in mk.values()]
    print(f"  live part: median {statistics.median(dur) / 60:.1f} min; median spread while live {statistics.median(spreads) * 100:.1f}c; quotes per market {statistics.median(len(m['q']) for m in mk.values()):.0f}")

    # calibration
    print("  calibration (book within 6c): side won vs its price")
    for off in (60, 180, 300):
        rows = []
        for tk, m in mk.items():
            t0 = m["start"] + off
            if t0 > m["ts"][-1] - 20:
                continue
            i = bisect.bisect_right(m["ts"], t0) - 1
            _, b, a = m["q"][i]
            if b <= 0 or a - b > 0.06:
                continue
            ask = a
            cost = ask + fee(ask)
            rows.append({"event": m["event"], "mid": (a + b) / 2, "win": m["win"], "hold": (1 - cost) / cost if m["win"] else -1.0})
        if len(rows) < 30:
            print(f"    +{off // 60} min: only {len(rows)} usable"); continue
        out = []
        for lab, lo, hi in (("under 40c", 0, .40), ("40-60c", .40, .60), ("over 60c", .60, 1.01)):
            g = [r for r in rows if lo <= r["mid"] < hi]
            if len(g) >= 15:
                out.append(f"{lab}: price {statistics.mean(r['mid'] for r in g):.2f}, won {statistics.mean(r['win'] for r in g):.0%}, edge {100 * statistics.mean(r['win'] - r['mid'] for r in g):+.1f}±{se(g, lambda r: r['win'] - r['mid']):.1f}, hold {statistics.mean(r['hold'] for r in g):+.1%} (n={len(g)})")
        print(f"    +{off // 60} min | " + " | ".join(out))

    # dips
    rows = []
    for tk, m in mk.items():
        q, ts = m["q"], m["ts"]
        t0 = m["start"] + 120
        while t0 < ts[-1] - 180:
            i = bisect.bisect_right(ts, t0) - 1
            t0 += 15
            lo_i = bisect.bisect_left(ts, ts[i] - 120)
            _, b, a = q[i]
            if b <= 0 or not (0.25 <= a <= 0.75) or a - b > 0.10 or i - lo_i < 3:
                continue
            peak = max(r[1] for r in q[lo_i:i + 1])
            cost = a + fee(a)
            sold = None
            for j in range(i + 1, len(q)):
                if q[j][1] >= a + 0.08:
                    sold, bounced = q[j][1], True
                    break
                if ts[j] - ts[i] >= 180:
                    sold, bounced = q[j][1], False
                    break
            if sold is None:
                continue
            hold = ((1 - cost) / cost) if m["win"] else -1.0
            rows.append({"event": m["event"], "dip": peak - b >= 0.08, "bounce": bounced, "roi": (sold - fee(sold)) / cost - 1, "hold": hold, "spread": a - b})
    if rows:
        print(f"  dip replay: {len(rows)} sampled entries, average spread {statistics.mean(r['spread'] for r in rows) * 100:.1f}c")
        for lab, sel in (("after a dip (8c+ under the 2-min high)", [r for r in rows if r["dip"]]), ("every other moment", [r for r in rows if not r["dip"]])):
            if len(sel) >= 30:
                print(f"    {lab}: n={len(sel)}, bounces +8c {statistics.mean(r['bounce'] for r in sel):.0%}, sell-on-bounce return {statistics.mean(r['roi'] for r in sel):+.1%} ±{se(sel, lambda r: r['roi']):.1f}, hold to the result {statistics.mean(r['hold'] for r in sel):+.1%} ±{se(sel, lambda r: r['hold']):.1f}")
            else:
                print(f"    {lab}: only {len(sel)}")
