"""Pull the settlement indexes' 1 s series over a span, for a replay that needs the hour before each market's open (the wave readings of wavefacts.py: the typical minute, the usual leg and the zig-zag since the open). Rows only, read-only SQL through scripts/cloud.sh sql in two-hour pieces per index (nothing runs on the host but the queries). Each second keeps its last tick.
Run on the Mac from the repo: .venv/bin/python scripts/priskp_index_tape_pull.py "2026-11-25 15:25:00-05" "2026-11-27 00:00:00-05" OUT.json.gz [scripts/cloud.sh]
The file: {index_id: [[seconds...], [values...]]}."""
import datetime
import gzip
import json
import subprocess
import sys

from kalshi_moonshot.kalshiws import INDEX_FOR

START, END, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
CLOUD = sys.argv[4] if len(sys.argv) > 4 else "scripts/cloud.sh"


def sql(q: str) -> list[str]:
    body = "\\pset format unaligned\n\\pset tuples_only on\n" + q.rstrip(";") + ";\nSELECT '#END#';"
    for _ in range(6):
        hold = subprocess.Popen(["sleep", "300"], stdout=subprocess.PIPE)  # the session ends early when its input is at end-of-file, so it is given one that stays open
        try:
            out = subprocess.run([CLOUD, "sql", body], stdin=hold.stdout, capture_output=True, text=True).stdout.splitlines()
        finally:
            hold.kill()
        if "#END#" in out:
            return [x for x in out[: out.index("#END#")] if x and not x.startswith(("Output format", "Tuples only"))]
    raise SystemExit("the query did not come back whole: " + q[:120])


def epoch(text: str) -> int:
    return int(datetime.datetime.fromisoformat(text.replace(" ", "T")[:19] + text[19:].replace("-05", "-05:00").replace("+00", "+00:00")).timestamp())


a, b = epoch(START), epoch(END)
out: dict[str, list[list]] = {}
for ix in sorted(set(INDEX_FOR.values())):
    T: list[int] = []
    V: list[float] = []
    for lo in range(a, b, 7200):
        hi = min(lo + 7200, b)
        rows = sql(f"SELECT string_agg(s||':'||v, ',' ORDER BY s) FROM (SELECT DISTINCT ON (floor(ts)) floor(ts)::bigint s, value v FROM tape.index_ticks WHERE index_id='{ix}' AND ts>={lo} AND ts<{hi} ORDER BY floor(ts), ts DESC) x")
        for pair in (rows[0].split(",") if rows and rows[0] else []):
            s, v = pair.split(":")
            T.append(int(s))
            V.append(float(v))
    out[ix] = [T, V]
    print(ix, len(T), "seconds", file=sys.stderr)
gzip.open(OUT, "wt").write(json.dumps(out, separators=(",", ":")))
print("wrote", OUT, file=sys.stderr)
