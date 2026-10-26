"""Buy at the ask and pay the fee, or rest a bid at the current bid and pay none? Replayed from the tape. Places nothing.

Resting orders pay no trading fee on the series we trade (every one is fee type `quadratic`, and the app shows $0 at the resting price levels), so a bid posted at the bid saves the fee and the spread. What it can cost is
fills: it fills only when the market comes down to it. Per market side, at the first moment its ask is in the band (book within 8c):
  taker   buy at the ask now, with the fee, hold to the result
  maker   post at the current bid; filled if, within 3 minutes, the side's ask falls to that price (the market trades through us: the cautious fill rule, no queue luck); entry price = our bid, no fee; hold to the result
Reported per band: how often the maker order fills, its return per fill, its return per opportunity (an unfilled order earns zero), against the taker return on the same moments.
Standard errors across clusters (one 15-minute slot across coins, or one match).
    docker compose exec -T bot python - < scripts/maker_bid_study.py
"""
import bisect
import math
import statistics
import time
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
client = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
WAIT = 180.0
BANDS = (("toss-up 40-60c", 0.40, 0.60), ("favorite 70-90c", 0.70, 0.90))
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
FX = ("KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M")
COMMODITY = ("KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXCOPPER15M", "KXNATGAS15M", "KXPLATINUM15M", "KXPALLADIUM15M")


def rows_for(q, win_yes, cluster):
    out = []
    ts = [r[0] for r in q]
    for side in ("yes", "no"):
        bid = [r[1] if side == "yes" else 1 - r[2] for r in q]
        ask = [r[2] if side == "yes" else 1 - r[1] for r in q]
        win = win_yes if side == "yes" else not win_yes
        for bname, lo, hi in BANDS:
            for i in range(len(q)):
                if not (lo <= ask[i] <= hi) or ask[i] - bid[i] > 0.08 or bid[i] <= 0 or ts[i] < ts[0] + 60:
                    continue
                a, b = ask[i], bid[i]
                taker_cost = a + fee(a)
                taker = ((1 - taker_cost) / taker_cost) if win else -1.0
                fill = next((j for j in range(i + 1, bisect.bisect_right(ts, ts[i] + WAIT)) if ask[j] <= b + 1e-9), None)
                maker = None if fill is None else ((1 - b) / b if win else -1.0)
                out.append({"band": bname, "cl": cluster, "ask": a, "bid": b, "win": win, "taker": taker, "maker": maker})
                break
    return out


def show(name, rows):
    print(f"\n{name}")
    for bname, _, _ in BANDS:
        g = [r for r in rows if r["band"] == bname]
        if len(g) < 15:
            print(f"  {bname}: only {len(g)} moments")
            continue
        filled = [r for r in g if r["maker"] is not None]
        per = defaultdict(list)
        for r in filled:
            per[r["cl"]].append(r["maker"])
        mm = [sum(v) / len(v) for v in per.values()]
        se = math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) * 100 if len(mm) > 1 else float("nan")
        opp = statistics.mean((r["maker"] if r["maker"] is not None else 0.0) for r in g)
        print(f"  {bname}: {len(g)} moments | taker at the ask: price {statistics.mean(r['ask'] for r in g):.2f}, won {statistics.mean(r['win'] for r in g):.0%}, return {statistics.mean(r['taker'] for r in g):+.1%}"
              f" | maker at the bid: fills {len(filled) / len(g):.0%}, price {statistics.mean(r['bid'] for r in filled):.2f}, won {statistics.mean(r['win'] for r in filled):.0%} of fills, return per fill {statistics.mean(r['maker'] for r in filled):+.1%} ±{se:.1f}, per opportunity {opp:+.1%}")


def run_15m(name, series):
    rows = []
    for r in main.db.execute(f"SELECT ticker, result FROM windows WHERE series IN ({','.join('?' * len(series))}) AND result IN ('yes','no')", tuple(series)).fetchall():
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (r["ticker"],)).fetchall()
        if len(q) >= 40:
            rows += rows_for(q, r["result"] == "yes", r["ticker"].split("-")[1])
    show(name, rows)


run_15m("crypto 15-minute", CRYPTO)
run_15m("FX 15-minute", FX)
run_15m("commodity 15-minute", COMMODITY)
for name, prefixes in (("CS2 and Dota 2", ("KXCS2GAME", "KXDOTA2GAME")), ("eSoccer", ("KXESOCCERGAME",)), ("eBasketball", ("KXEBASKETBALLGAME",))):
    rows = []
    for prefix in prefixes:
        for (tk,) in tape.execute("SELECT ticker FROM quotes WHERE ticker LIKE ? GROUP BY ticker HAVING MAX(ts) < ? AND COUNT(*) >= 40", (prefix + "-%", time.time() - 1800)).fetchall():
            try:
                res = client.market(tk).get("result")
            except Exception:
                continue
            time.sleep(0.3)
            if res not in ("yes", "no"):
                continue
            q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (tk,)).fetchall()
            if len(q) >= 40:
                rows += rows_for(q, res == "yes", tk.rsplit("-", 1)[0])
    show(name, rows)
