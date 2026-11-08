"""The lane's own record by minutes into the window, as timerecord reads it now, and what its shadow log says (2026-11-08, user: "it's not betting at the right times ... as if it doesn't realize there's only a few minutes left"). Read-only.
    scripts/cloud.sh run scripts/timing_record_now.py
"""

import json
from pathlib import Path

from kalshi_moonshot import config, timerecord
from kalshi_moonshot.store import Store

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
rec = timerecord.record(st, "crypto_multiscale")
print("bucket (minutes into the window) : settled bets, shrunk net per $1 staked, sits out?")
for b in sorted(rec):
    n, score = rec[b]
    out = n >= timerecord.MIN_N and score < 0
    print(f"   {b * 2:2d}-{b * 2 + 2:2d} min : n={n:3d} score {score:+.3f} {'SITS OUT' if out else ''}")
p = Path(s.data_dir) / "timing_shadow.jsonl"
rows = []
if p.exists():
    for line in p.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
print("\nshadow log rows:", len(rows))
kinds = {}
for r in rows:
    k = r.get("kind")
    kinds[k] = kinds.get(k, 0) + 1
print("by kind:", kinds)
for r in [x for x in rows if x.get("kind") == "score"][-6:]:
    print(r)

sc = [r for r in rows if r.get("kind") == "score"]
for skip in (True, False):
    xs = [r["net"] for r in sc if r.get("would_skip") is skip]
    if xs:
        m = sum(xs) / len(xs)
        se = (sum((x - m) ** 2 for x in xs) / max(len(xs) - 1, 1)) ** 0.5 / len(xs) ** 0.5
        print(f"shadow, would {'SKIP' if skip else 'BUY '}: n={len(xs)} net per $1 {m:+.3f} (se {se:.3f}) won {100 * sum(1 for x in xs if x > 0) / len(xs):.0f}%")
print("DONE")
