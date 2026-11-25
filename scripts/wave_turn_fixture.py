"""Build tests/fixtures/wave_turn_marks.json.gz: the user's marked wave markets and 'no good buy' markets (2026-11-24) with the spot path each was
judged on, for tests/test_wave_turn.py's replay of the study (scripts/wave_turn_chop.py, branch wave-turn-chop commit 3232730).

Per market: id, ticker, target (the step's own target at the time), hour (the 1 s spot path of the hour before the open, from the wave-scale pull),
v (the 1 s path of seconds 0..900 of the window), the user's buys and the nogood flag. Inputs (from that branch):
  LABELS=studies/results/2026-11-24-wave-user-moments-labels.json CURVES=...-curves.json HOUR=studies/results/2026-11-24-wave-scale-labels.json.gz
Run on the Mac: LABELS=... CURVES=... HOUR=... .venv/bin/python scripts/wave_turn_fixture.py. Read-only.
"""

import gzip
import json
import os
from pathlib import Path

lab = json.load(open(os.environ["LABELS"]))["markets"]
cur = {m["id"]: m for m in json.load(open(os.environ["CURVES"]))["markets"]}
hour = json.load(gzip.open(os.environ["HOUR"], "rt"))
rows = []
for mid in sorted(lab):
    c = cur[mid]
    rows.append({"id": mid, "ticker": c["ticker"], "target": c["target"], "hour": hour[c["ticker"]]["g"][:3600], "v": c["v"], "buy": lab[mid]["buy"], "nogood": lab[mid]["nogood"]})
out = Path("tests/fixtures/wave_turn_marks.json.gz")
with gzip.open(out, "wt") as fh:
    json.dump(rows, fh)
print(f"{len(rows)} markets, {out.stat().st_size} bytes")
