"""Build tests/fixtures/wave_shape_labels.json.gz: the user's labelled wave buys (memory: project-wave-buy-shape-labels-2026-11-24) with the Kalshi spot path each was judged on, for tests/test_wave_shape.py's replay.

Source: the study's data dump (scripts/wave_buy_shape_classifier.py on the jobs runner, job 20261124-103238-52855: wave_buy_shape_data.json.gz) and its host pull of ETH 10:30 on 2026-11-24 (studies/results/2026-11-24-wave-buy-shape-extra.json.gz, branch commit ddd4ff0). Each row keeps the path from an hour before the open to the buy's second, the target, the side, the label and the study's verdict (branch, or "" for a fail).

Run on the Mac: DATA=<dump> EXTRA=<extra> STUDY=<the study script> .venv/bin/python scripts/wave_shape_fixture.py. Read-only.
"""

import contextlib
import gzip
import io
import json
import os
from pathlib import Path

ns = {"__name__": "study"}
os.environ["EXTRA_DATA"] = os.environ["EXTRA"]
with contextlib.redirect_stdout(io.StringIO()):
    exec(Path(os.environ["STUDY"]).read_text(), ns)  # the study's own measure and judge, on the dump named by DATA
PRE = ns["PRE"]
rows = []


def keep(tk, m, side, ts, label, branch):
    opened = m["close"] - 900
    t = int(float(ts) - opened)
    rows.append({"ticker": tk, "index": m["index"], "close": m["close"], "K": m["K"], "side": side, "t": t, "label": label, "branch": branch, "g": m["g"][: PRE + t + 1]})


for r in ns["bets"]:
    if r["label"] in ("Q", "N", "D"):
        keep(r["ticker"], ns["M"][r["ticker"]], r["side"], r["ts"], r["label"], r["branch"])
for tk, _ix, close, _K, _labs in ns["EXTRA"]:
    m = ns["M"][tk]
    for label, k in (("Q", 260), ("Q2", 481), ("late", 652)):  # ETH 10:30: dip 1 (passes 251-289 on S), dip 2 (passes 457-491 on R), the third low (passes nowhere)
        rr = {"ts": close - 900 + k, "ticker": tk, "side": "yes", "ask": 0.2}
        ns["apply"](rr, m)
        keep(tk, m, "yes", rr["ts"], label, rr["branch"])
out = Path("tests/fixtures/wave_shape_labels.json.gz")
out.parent.mkdir(exist_ok=True)
with gzip.open(out, "wt") as fh:
    json.dump(rows, fh)
print(f"{len(rows)} rows, {out.stat().st_size} bytes: " + ", ".join(f"{r['ticker'].split('-')[0]} {r['label']} {r['branch'] or 'fail'}" for r in rows))
