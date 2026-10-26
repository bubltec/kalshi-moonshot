"""FX BURSTS: how thin are the FX 15-minute markets next to crypto, and would the burst rule (last-3-s volume >= 5x the market's own median second of the previous 2 minutes, 60%+ one side, 300+ contracts) ever fire there? Prints the 19:30 USD pairs' busiest seconds and, per family, the share of windows with a ratio-5 burst at several contract floors. Reads flow_1s; places nothing.
    docker compose exec -T bot python - < scripts/fx_burst_study.py
"""
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
FAM = {"fx": ("KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M"), "crypto": ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M"), "commodity": ("KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXBRENT15M", "KXNATGAS15M", "KXCOPPER15M")}


def bursts(book):
    """book: second -> (yes_vol, no_vol). Yields (sec, ratio, share, total3, against_side) for every second where the 3-s volume is a burst by ratio and share."""
    out = []
    for sec in sorted(book):
        tot = [0.0, 0.0]
        for k in range(3):
            r = book.get(sec - k)
            if r:
                tot[0] += r[0]
                tot[1] += r[1]
        total = tot[0] + tot[1]
        per = sorted(sum(book.get(sec - 3 - k, (0.0, 0.0))) for k in range(120))
        base = (per[59] + per[60]) / 2.0
        ratio = total / (3.0 * base + 1.0)
        if total and ratio >= 5.0:
            hi = max(tot) / total
            out.append((sec, ratio, hi, total, "yes" if tot[0] >= tot[1] else "no"))
    return out


print("busiest seconds, 19:30 USD pairs (UTC 23:15-23:30)")
for tk in ("KXUSDJPY15M-26OCT251930-30", "KXGBPUSD15M-26OCT251930-30", "KXEURUSD15M-26OCT251930-30", "KXUSDCAD15M-26OCT251930-30"):
    rows = tape.execute("SELECT ts, yes_vol, no_vol FROM flow_1s WHERE ticker=? ORDER BY ts", (tk,)).fetchall()
    book = {r[0]: (r[1], r[2]) for r in rows}
    tot = sum(sum(v) for v in book.values())
    top = sorted(book.items(), key=lambda kv: -sum(kv[1]))[:4]
    print(tk, f"{len(rows)} active seconds, {tot:.0f} contracts in all; top seconds:", [(sec % 1000, round(v[0]), round(v[1])) for sec, v in top])
    for b in bursts(book)[:3]:
        print("   ratio>=5:", b[0] % 1000, f"ratio {b[1]:.0f} share {b[2]:.2f} total3 {b[3]:.0f} taker side {b[4]}")

print("\nper family: windows seen, median traded contracts per window, windows with a ratio>=5 / 60% burst at each contract floor")
for fam, series in FAM.items():
    wins = defaultdict(dict)
    for r in tape.execute("SELECT ticker, ts, yes_vol, no_vol FROM flow_1s WHERE " + " OR ".join("ticker LIKE ?" for _ in series) + " ORDER BY ticker, ts", tuple(sr + "-%" for sr in series)).fetchall():
        wins[r[0]][r[1]] = (r[2], r[3])
    vols = [sum(sum(v) for v in b.values()) for b in wins.values()]
    if not vols:
        continue
    line = []
    for floor in (20, 50, 100, 300):
        n = sum(1 for b in wins.values() if any(x[2] >= 0.6 and x[3] >= floor for x in bursts(b)))
        line.append(f"floor {floor}: {n}/{len(wins)}")
    print(f"{fam:10} {len(wins)} windows, median {statistics.median(vols):.0f} contracts per window; " + "; ".join(line))
