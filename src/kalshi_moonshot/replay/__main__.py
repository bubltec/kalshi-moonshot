"""python -m kalshi_moonshot.replay capture TICKER LANE [--name N] [--note T] | run TAPE.json... --lanes strategies.toml | list DIR"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import harness
from .tape import Tape, capture


def _from_db(days: float, lane: str, series: list[str] | None = None, as_lane: str = "") -> list[Tape]:
    """Every settled bet of `lane` (of `series`, if given) in the last `days` days that was held to the close, as a tape of lane `as_lane`."""
    import time

    from .. import config, db
    from ..store import Store

    s = config.reload()
    store, tape_db = Store(s.db_path, s.exclude_versions), db.connect(s.tape_path, "tape", shared=True)
    rows = store.db.execute(
        "SELECT d.ticker FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.action='bet' AND d.mode='trade' AND d.lane=? AND d.exit_price IS NULL AND d.outcome IN ('yes','no') AND d.ts>?",
        (lane, time.time() - days * 86400.0),
    ).fetchall()
    out = []
    for r in rows:
        try:
            t = capture(store, tape_db, r["ticker"], lane)
        except SystemExit:
            continue
        if series and r["ticker"].split("-")[0] not in series:
            continue
        if as_lane:
            t.lane = as_lane
        if t.ref > 0 and len(t.ticks) >= 60 and len(t.quotes) >= 20:  # a bet from before the spot feed was recorded cannot be replayed
            out.append(t)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m kalshi_moonshot.replay")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture", help="build a tape from the databases (run where the bot's data is) and print it packed, or save it with --out")
    c.add_argument("ticker")
    c.add_argument("lane")
    c.add_argument("--name", default="")
    c.add_argument("--note", default="")
    c.add_argument("--out", type=Path)
    r = sub.add_parser("run", help="replay tapes under a lane file and print one line each")
    r.add_argument("tapes", nargs="+", type=Path)
    r.add_argument("--lanes", type=Path, required=True)
    st = sub.add_parser("study", help="sweep a study file (studies/*.yaml) over tapes: a directory of tape files, or the bot's own held bets with --since")
    st.add_argument("file", type=Path)
    st.add_argument("--lanes", type=Path, required=True)
    st.add_argument("--tapes", type=Path)
    st.add_argument("--since", action="store_true", help="capture the study's since_days of settled, held live bets from the databases instead of reading --tapes")
    st.add_argument("--csv", action="store_true", help="print every row as CSV (for a run on the host, analysed elsewhere)")
    bd = sub.add_parser("board", help="print the strategy board (studies/concepts.yaml) grouped by status")
    bd.add_argument("file", type=Path, nargs="?", default=Path("studies/concepts.yaml"))
    ls = sub.add_parser("list", help="list the tapes in a directory")
    ls.add_argument("dir", type=Path)
    a = ap.parse_args()
    if a.cmd == "capture":
        from .. import config, db
        from ..store import Store

        s = config.reload()
        t = capture(Store(s.db_path, s.exclude_versions), db.connect(s.tape_path, "tape", shared=True), a.ticker, a.lane, a.name, a.note)
        if a.out:
            t.save(a.out)
            print(f"saved {a.out}: {len(t.quotes)} quotes, {len(t.ticks)} ticks")
        else:
            print("PACKED:" + t.pack())
    elif a.cmd == "run":
        lanes = a.lanes.read_text()
        for p in a.tapes:
            o = harness.run(Tape.load(p), lanes)
            print(f"{o.tape:44s} signals {o.signals:4d} {'SOLD' if o.sold else 'held'} {'' if o.exit_price is None else f'@{o.exit_price:.2f} {o.exit_left:.0f}s left'} sold {o.sold_pnl:+.2f} hold {o.hold_pnl:+.2f} delta {o.delta:+.2f}{'  STUCK' if o.stuck else ''}")
    elif a.cmd == "board":
        from . import studies

        order = ["promising", "needs_work", "untested", "dead"]
        by: dict[str, list] = {k: [] for k in order}
        for row in studies.concepts(a.file):
            by.setdefault(row["status"], []).append(row)
        for status in order:
            print(f"\n== {status.replace('_', ' ')} ({len(by[status])})")
            for row in by[status]:
                print(f"  {row['id']:34s} {row.get('lane', ''):18s} {row.get('evidence', '')[:90]}" + (f"\n  {'':34s} next: {row['next']}" if row.get("next") else ""))
    elif a.cmd == "study":
        from . import studies
        from .sweep import summarize, sweep

        study = studies.load(a.file)
        grid = study.grid
        tapes = _from_db(study.since_days, study.bets_lane, study.series, study.lane) if a.since else [Tape.load(p) for p in sorted(a.tapes.glob("*.json"))]
        rows = sweep(tapes, a.lanes.read_text(), grid, study.lane, study.fixed)
        if a.csv:
            import csv
            import sys

            w = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        else:
            print(summarize(rows, list(grid)).to_string())
    else:
        for p in sorted(a.dir.glob("*.json")):
            t = Tape.load(p)
            print(f"{p.name:44s} {t.ticker} {t.lane} {t.side} entry {t.entry:.2f} result {t.result or '-'}  {t.note}")


if __name__ == "__main__":
    main()
