"""The sides the top-of-range rule skipped: what tells the winners from the losers? (2026-11-16, user: "how do we get this corrected so it is not skipping winners")

One question on the paper lane's own decisions (crypto_multiscale_rest since 2026-11-16 09:06 ET, wave_quote with wave_pos_max 0.8): each side skipped for sitting in the top fifth of its range and never bought, taken at its FIRST skip, held to settlement at that ask, fee in. Split by the readings its note carried at that moment: the ask, the minutes into the market, how far up its range it sat, the range's size in typical minutes, the curve edge, and (from 12:00, when the note began to carry it) the 3-hour spike reading. A side "pays" when it wins more often than its ask plus the fee needs, not when it wins more often than not. Bands fixed before the run. No coin is named.

Run on the host: scripts/cloud.sh run scripts/top_skip_winners.py
"""
import datetime
import re

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 16, 9, 6, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
res = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE - 900,)).fetchall()}
bought, skip = set(), {}
for d in st.execute("SELECT ts, ticker, side, ask, action, reason FROM decisions WHERE lane='crypto_multiscale_rest' AND ts>? ORDER BY ts", (SINCE,)).fetchall():
    k, why = (d["ticker"], d["side"]), str(d["reason"] or "")
    if d["action"] == "bet":
        bought.add(k)
    elif "waits for the low" in why and k not in skip:
        skip[k] = (float(d["ts"]), float(d["ask"] or 0), why)
rows = []
for k, (ts, ask, why) in skip.items():
    if k in bought or k[0] not in res or not (0.05 < ask < 0.99):
        continue
    result, close = res[k[0]]
    g = lambda pat: (lambda m: float(m.group(1)) if m else None)(re.search(pat, why))  # noqa: E731
    sim = re.search(r"\[spike view z ([+-][\d.]+), (\d+) of", why)
    rows.append({"ask": ask, "won": result == k[1], "min": (ts - (close - 900.0)) / 60.0, "pos": g(r"\[wave amp [\d.]+ pos ([\d.]+)"), "amp": g(r"\[wave amp ([\d.]+)"),
                 "edge": g(r"\[curve edge ([+-][\d.]+)\]"), "z": float(sim.group(1)) if sim else None, "similar": float(sim.group(2)) if sim else None})


def line(name, rs):
    if len(rs) < 8:
        print(f"  {name:30s} n {len(rs):3d}")
        return
    n = len(rs)
    need = sum(r["ask"] + fee(r["ask"]) for r in rs) / n
    net = sum((1.0 if r["won"] else 0.0) / (r["ask"] + fee(r["ask"])) - 1.0 for r in rs) / n * 100
    print(f"  {name:30s} n {n:3d} | won {sum(1 for r in rs if r['won']) / n:4.0%} | needs {need:4.0%} | ask {sum(r['ask'] for r in rs) / n:.3f} | held net per $ {net:+6.1f}%")


def bands(title, key, cuts):
    print(f"\n{title}")
    none = [r for r in rows if r[key] is None]
    if none:
        line("no reading", none)
    for lo, hi, name in cuts:
        line(name, [r for r in rows if r[key] is not None and lo <= r[key] < hi])


print(f"window: crypto_multiscale_rest since 2026-11-16 09:06 ET; {len(rows)} settled sides skipped at the top and never bought")
line("all", rows)
bands("1. by the ask", "ask", ((0, 0.70, "under 0.70"), (0.70, 0.78, "0.70 to 0.78"), (0.78, 0.84, "0.78 to 0.84"), (0.84, 1, "0.84 and over")))
bands("2. by minutes into the market", "min", ((0, 3, "under 3"), (3, 6, "3 to 6"), (6, 9, "6 to 9"), (9, 15, "9 and over")))
bands("3. by how far up its range the side sat", "pos", ((0.8, 0.9, "0.80 to 0.90"), (0.9, 0.995, "0.90 to 0.99"), (0.995, 2, "at the very top (1.00)")))
bands("4. by the size of the range (typical minutes)", "amp", ((0, 3, "under 3"), (3, 5, "3 to 5"), (5, 99, "5 and over")))
bands("5. by the curve edge (the curve's chance less the ask)", "edge", ((-9, -0.03, "under -0.03"), (-0.03, 0, "-0.03 to 0"), (0, 9, "0 and over")))
bands("6. by the 3-hour spike reading, signed toward the side (from 12:00)", "z", ((-99, 0.5, "under +0.5"), (0.5, 1.5, "+0.5 to +1.5"), (1.5, 99, "+1.5 and over")))
bands("7. by similar moves in the last 3 hours (from 12:00)", "similar", ((0, 6, "5 or fewer (a rare move)"), (6, 25, "6 to 24"), (25, 121, "25 and over (ordinary)")))
print("DONE")
