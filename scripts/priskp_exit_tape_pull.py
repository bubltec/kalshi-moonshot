"""Pull, for the one-fall-rule replay (scripts/priskp_fall_rule_replay.py), the live PriskP bets since the per-tick entry change (2026-11-25 15:57 ET) with the tape each one needs. Rows only, no study, read-only SQL through scripts/cloud.sh sql in small chunks (nothing runs on the host but the queries): per bet the decision (side, price paid, buy and exit time, exit price and reason), the market's strike and result (windows), the market's quotes on a 1 s grid from its open to its close (bid, ask; the last quote carried forward) and the settlement index on a 1 s grid from 620 s before the open to the close.
Run on the Mac from the repo: .venv/bin/python scripts/priskp_exit_tape_pull.py OUT.json.gz"""
import gzip
import json
import re
import subprocess
import sys

from kalshi_moonshot.kalshiws import index_for_series

CUT = "2026-11-25 15:57:00-05"
CLOUD = sys.argv[2] if len(sys.argv) > 2 else "scripts/cloud.sh"


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


def grid(pairs: list[tuple[int, float]], a: int, b: int) -> list[float | None]:
    """Values on the seconds a+1..b, the last one carried forward (at most 10 s into the grid's first second)."""
    out: list[float | None] = []
    last, i = None, 0
    while i < len(pairs) and pairs[i][0] <= a:
        if a - pairs[i][0] <= 10:
            last = pairs[i][1]
        i += 1
    for k in range(a + 1, b + 1):
        while i < len(pairs) and pairs[i][0] <= k:
            last = pairs[i][1]
            i += 1
        out.append(last)
    return out


bets = []
for line in sql(f"SELECT d.ticker, d.side, d.ts, d.stake_usd/d.contracts, d.contracts, coalesce(d.exit_ts::text,''), coalesce(d.exit_price::text,''), replace(left(coalesce(d.exit_reason,''),40),'|','/'), w.close_ts, coalesce(w.strike::text,''), coalesce(w.result,'') FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.lane='priskp' AND d.action='bet' AND d.mode='trade' AND d.stake_usd>0 AND d.ts>=extract(epoch from timestamptz '{CUT}') ORDER BY d.ts"):
    f = line.split("|")
    bets.append({"ticker": f[0], "side": f[1], "ts": float(f[2]), "entry": float(f[3]), "n": float(f[4]), "exit_ts": float(f[5]) if f[5] else None, "exit_price": float(f[6]) if f[6] else None, "exit_reason": f[7], "close": int(float(f[8])), "strike": float(f[9]) if f[9] else None, "result": f[10]})
print(len(bets), "bets", file=sys.stderr)
tickers = sorted({b["ticker"] for b in bets})
close = {b["ticker"]: b["close"] for b in bets}
quotes: dict[str, tuple[list, list]] = {}
for k in range(0, len(tickers), 12):
    part = tickers[k : k + 12]
    cond = " OR ".join(f"(ticker='{t}' AND ts>{close[t] - 912} AND ts<={close[t]})" for t in part)
    for line in sql(f"SELECT ticker, string_agg(floor(ts)::bigint||':'||round((bid*1000)::numeric)||':'||round((ask*1000)::numeric), ',' ORDER BY ts) FROM tape.quotes WHERE {cond} GROUP BY ticker"):
        t, agg = line.split("|")
        rows = [x.split(":") for x in agg.split(",")]
        quotes[t] = (grid([(int(r[0]), int(r[1]) / 1000.0) for r in rows], close[t] - 900, close[t]), grid([(int(r[0]), int(r[2]) / 1000.0) for r in rows], close[t] - 900, close[t]))
    print("quotes", k + len(part), "/", len(tickers), file=sys.stderr)
idx: dict[str, list] = {}
for k in range(0, len(tickers), 8):
    part = tickers[k : k + 8]
    cond = " OR ".join(f"(index_id='{index_for_series(re.match(r'(KX[A-Z0-9]+15M)', t).group(1))}' AND ts>{close[t] - 1532} AND ts<={close[t]})" for t in part)
    by_ix: dict[str, list[tuple[int, float]]] = {}
    for line in sql(f"SELECT index_id, string_agg(floor(ts)::bigint||':'||value, ',' ORDER BY ts) FROM tape.index_ticks WHERE {cond} GROUP BY index_id"):
        ix, agg = line.split("|")
        by_ix[ix] = [(int(a), float(b)) for a, b in (x.split(":") for x in agg.split(","))]
    for t in part:
        idx[t] = grid(by_ix.get(index_for_series(re.match(r"(KX[A-Z0-9]+15M)", t).group(1)), []), close[t] - 1520, close[t])
    print("index", k + len(part), "/", len(tickers), file=sys.stderr)
for b in bets:
    b["bid"], b["ask"] = quotes.get(b["ticker"], ([], []))
    b["idx"] = idx.get(b["ticker"], [])
gzip.open(sys.argv[1], "wt").write(json.dumps(bets, separators=(",", ":")))
print("wrote", sys.argv[1], file=sys.stderr)
