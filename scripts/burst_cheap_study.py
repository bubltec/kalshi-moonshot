"""Moonshot v2 replay: buy a 2-20c side in the last 120 s of a 15-minute market when THAT side's volume spikes.

Does not need a held favorite. 5x that side's own median second (not the mixed tape), 300 contracts,
median second at least 10. A 95c YES pile-on is ignored; a 5c NO dump is the burst. First trigger per
window-side. Compared with buying the first time that side is 2-20c in the last 120 s, spike or not.
    docker compose exec -T bot python - < scripts/burst_cheap_study.py
"""
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

RATIO, FLOOR, MIN_NORMAL = 5.0, 300.0, 10.0
LO, HI = 0.02, 0.20
WINDOW, LAST = 120, 3


def family(series: str) -> str:
    core = series[2:] if series.startswith("KX") else series
    if core.startswith(("BTC", "ETH", "SOL", "XRP", "DOGE")):
        return "crypto"
    if core.startswith(("EUR", "GBP", "USD", "AUD")):
        return "fx"
    return "commodity"


def roi(ask: float, won: bool) -> float:
    cost = ask + fee(ask)
    return (1.0 if won else 0.0) / cost - 1.0 if cost > 0 else 0.0


def summarize(rows: list[tuple], title: str) -> None:
    print(f"\n{title}")
    print(f"{'group':12}{'n':>6}{'wins':>6}{'win%':>7}{'avg ask':>9}{'edge':>8}{'ROI':>8}")
    by: dict[str, list] = defaultdict(list)
    for fam, ask, won in rows:
        by[fam].append((ask, won))
        by["all"].append((ask, won))
    for key in ("all", "crypto", "commodity", "fx"):
        xs = by.get(key) or []
        if not xs:
            print(f"{key:12}{0:6}")
            continue
        n, w = len(xs), sum(1 for _, won in xs if won)
        avg_ask = sum(a for a, _ in xs) / n
        wr = w / n
        r = sum(roi(a, won) for a, won in xs) / n
        print(f"{key:12}{n:6}{w:6}{wr:7.0%}{avg_ask:9.3f}{wr - avg_ask:+8.3f}{r:+8.1%}")


def main() -> None:
    s = config.reload()
    tape = db.connect(s.tape_path, "tape", shared=True)
    store = Store(s.db_path, s.exclude_versions)
    wins = store.db.execute(
        "SELECT ticker, series, close_ts, result FROM windows WHERE series LIKE ? AND result IN ('yes','no')",
        ("%15M",),
    ).fetchall()
    spike, cheap = [], []
    scanned = eligible = 0
    for m in wins:
        close = float(m["close_ts"])
        quotes = tape.execute(
            "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid>0 AND ask>0 ORDER BY ts",
            (m["ticker"], close - WINDOW - 5, close),
        ).fetchall()
        flow = tape.execute(
            "SELECT ts, yes_vol, no_vol FROM flow_1s WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts",
            (m["ticker"], int(close - WINDOW - 125), int(close)),
        ).fetchall()
        scanned += 1
        if len(quotes) < 10 or len(flow) < 20:
            continue
        qts = [r[0] for r in quotes]
        fby = {int(r[0]): (float(r[1]), float(r[2])) for r in flow}
        fam = family(m["series"])
        saw_cheap = False
        fired = {"yes": False, "no": False}
        logged_cheap = {"yes": False, "no": False}
        qi = 0
        for sec in range(WINDOW, LAST, -1):
            t = close - sec
            while qi + 1 < len(qts) and qts[qi + 1] <= t:
                qi += 1
            if qts[qi] > t or t - qts[qi] > 3:
                continue
            yes_bid, yes_ask = float(quotes[qi][1]), float(quotes[qi][2])
            asks = {"yes": yes_ask, "no": 1.0 - yes_bid}
            yes_v = no_v = 0.0
            yes_hist = []
            no_hist = []
            for k in range(3):
                f = fby.get(int(t) - k)
                if f:
                    yes_v += f[0]
                    no_v += f[1]
            for k in range(120):
                f = fby.get(int(t) - 3 - k, (0.0, 0.0))
                yes_hist.append(f[0])
                no_hist.append(f[1])
            yes_hist.sort()
            no_hist.sort()
            bases = {"yes": (yes_hist[59] + yes_hist[60]) / 2.0, "no": (no_hist[59] + no_hist[60]) / 2.0}
            vols = {"yes": yes_v, "no": no_v}
            for side, ask in asks.items():
                if not (LO <= ask <= HI):
                    continue
                saw_cheap = True
                won = m["result"] == side
                if not logged_cheap[side]:
                    logged_cheap[side] = True
                    cheap.append((fam, ask, won))
                with_ = vols[side]
                base = bases[side]
                ratio = with_ / (3.0 * base + 1.0)
                if fired[side]:
                    continue
                if ratio >= RATIO and with_ >= FLOOR and base >= MIN_NORMAL:
                    fired[side] = True
                    spike.append((fam, ask, won))
        if saw_cheap:
            eligible += 1
    print(f"{scanned} 15-minute windows, {eligible} had a 2-20c side in the last 120s")
    print(f"burst test: {RATIO:g}x that side's own median second (median >= {MIN_NORMAL:g}), {FLOOR:g} contracts in 3s")
    summarize(spike, "buy the cheap side on the first volume spike")
    summarize(cheap, "buy the first time the side is 2-20c (no volume test)")


if __name__ == "__main__":
    main()
