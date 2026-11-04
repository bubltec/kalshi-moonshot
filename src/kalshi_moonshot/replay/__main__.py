"""python -m kalshi_moonshot.replay capture TICKER LANE [--name N] [--note T] | run TAPE.json... --lanes strategies.toml | list DIR"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import harness
from .tape import Tape, capture


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
    else:
        for p in sorted(a.dir.glob("*.json")):
            t = Tape.load(p)
            print(f"{p.name:44s} {t.ticker} {t.lane} {t.side} entry {t.entry:.2f} result {t.result or '-'}  {t.note}")


if __name__ == "__main__":
    main()
