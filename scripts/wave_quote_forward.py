"""The paper rule scored forward: crypto_multiscale_rest with wave_quote and wave_pos_max 0.8, since it was set (2026-11-16 09:06 ET; user: "lets see what it buys over the next hour").

One question on the lane's own decisions (CLAUDE.md rule 4 and 5): the buys the rule let through against the buys it skipped (a pass whose note says the buy sat above 0.8 of the side's range), each held to settlement at the ask it was decided at, fee in. A side skipped and later bought in the same market counts as bought. Only settled markets count.

Run on the host: scripts/cloud.sh run scripts/wave_quote_forward.py     (SINCE=epoch to move the start)
"""
import datetime
import os
import re

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = float(os.environ.get("SINCE") or datetime.datetime(2026, 11, 16, 9, 6, tzinfo=ET).timestamp())
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
res = {w["ticker"]: w["result"] for w in st.execute("SELECT ticker, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE - 900,)).fetchall()}
rows = st.execute("SELECT ts, ticker, side, ask, action, reason FROM decisions WHERE lane='crypto_multiscale_rest' AND ts>? ORDER BY ts", (SINCE,)).fetchall()
bought, skipped, unread = {}, {}, 0
for r in rows:
    k, why = (r["ticker"], r["side"]), str(r["reason"] or "")
    m = re.search(r"\[wave amp [\d.]+ pos ([\d.]+)", why)
    if r["action"] == "bet":
        bought.setdefault(k, (float(r["ts"]), float(r["ask"] or 0), float(m.group(1)) if m else None))
        unread += "[wave unreadable" in why
    elif "waits for the low" in why and m:
        skipped.setdefault(k, (float(r["ts"]), float(r["ask"] or 0), float(m.group(1))))
skipped = {k: v for k, v in skipped.items() if k not in bought}


def line(name, d):
    rs = [(v[1], res[k[0]] == k[1]) for k, v in d.items() if k[0] in res and 0.05 < v[1] < 0.99]
    if not rs:
        print(f"  {name:34s} decided {len(d):3d}, settled   0")
        return
    net = sum((1.0 if w else 0.0) / (a + fee(a)) - 1.0 for a, w in rs) / len(rs)
    print(f"  {name:34s} decided {len(d):3d}, settled {len(rs):3d} | won {sum(1 for _, w in rs if w) / len(rs):4.0%} | ask {sum(a for a, _ in rs) / len(rs):.3f} | held net per $ {net * 100:+6.1f}%")


hm = lambda t: datetime.datetime.fromtimestamp(t, ET).strftime("%H:%M:%S")  # noqa: E731
print(f"crypto_multiscale_rest since {hm(SINCE)} ET: {len(rows)} decisions")
line("bought (let through)", bought)
line("  of those, position under 0.4", {k: v for k, v in bought.items() if v[2] is not None and v[2] < 0.4})
line("  of those, position 0.4 to 0.8", {k: v for k, v in bought.items() if v[2] is not None and v[2] >= 0.4})
line("  of those, no reading", {k: v for k, v in bought.items() if v[2] is None})
line("skipped at the top, never bought", skipped)
print("\nthe buys:")
for k, v in sorted(bought.items(), key=lambda x: x[1][0]):
    print(f"  {hm(v[0])} {k[0]:28s} {k[1]:3s} ask {v[1]:.2f} pos {'n/a ' if v[2] is None else format(v[2], '.2f')} -> {'won' if res.get(k[0]) == k[1] else 'lost' if k[0] in res else 'open'}")
print("DONE")
