"""The review's numbers, in one read-only report: money, each lane's record, what the exits cost or save, calibration, data and infrastructure health.

`python -m kalshi_moonshot digest [--days N]` prints it for the last N days (the scheduled review reads the last day and the last week). A person or
the scheduled review reads it next to the git log and the open items in DATA_DIR/review-notes.md (if that file exists), then decides what to change.
Nothing here changes a setting or places an order.
"""

from __future__ import annotations

import contextlib
import shutil
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from . import archive, db, lanes
from .config import Settings
from .strategy import fee_per_contract as fee

EXIT_SLIP = 0.02  # what a sale gives up against the bid, as in the replay studies


def _won(r: Any) -> float:
    return 1.0 if r["outcome"] == (r["side"] or "yes") else 0.0


def _roi(r: Any) -> tuple[float, float] | None:
    """(return as traded, return if held) per $1 staked for a settled bet, after fees. None if it has no ask or is unsettled."""
    if r["ask"] is None or r["outcome"] not in ("yes", "no"):
        return None
    cost = r["ask"] + fee(r["ask"])
    won = _won(r)
    held = won / cost - 1
    if r["exit_price"] is not None:
        return (r["exit_price"] - EXIT_SLIP - fee(r["exit_price"])) / cost - 1, held
    return held, held


def _pct(x: float | None) -> str:
    return "   -  " if x is None else f"{x:+6.1%}"


def _kind(reason: str | None) -> str:
    r = (reason or "held to settlement").lower()
    for key in ("stop-loss", "peak-crash", "hover", "unstable", "spike", "take-profit"):
        if r.startswith(key):
            return key
    return "other exit"


def lane_table(store: Any, since: float, s: Settings | None = None) -> list[str]:
    """One row per lane; a live lane is split by lane version (the newest first), because a lane that was tuned during the period is several different bets."""
    rows = store.db.execute("SELECT lane, mode, ask, side, outcome, exit_price, exit_reason, stake_usd, lane_ver FROM decisions WHERE action = 'bet' AND ts >= ?", (since,)).fetchall()
    current = {}
    if s is not None:
        current = {name: lanes.version_of(spec, s) for name, spec in lanes.lane_specs(s).items()}
    by: dict[tuple[str, str, str], list] = defaultdict(list)
    for r in rows:
        live = r["mode"] == "trade"
        by[(r["lane"] or "moonshot", "LIVE" if live else "paper", (r["lane_ver"] or "?") if live else "")].append(r)
    out = [f"{'lane':24}{'mode':6}{'version':>10}{'bets':>5}{'settled':>8}{'as traded':>11}{'if held':>9}{'exits cost':>11}{'win%':>6}{'staked $':>10}{'P&L $':>9}"]

    def first(rs: list) -> float:
        return min(float(r["ts"]) for r in rs) if rs and "ts" in rs[0].keys() else 0.0

    for (lane, mode, ver), rs in sorted(by.items(), key=lambda kv: (kv[0][1] != "LIVE", kv[0][0], kv[0][2])):
        tag = (ver + (" (now)" if current.get(lane) == ver else "")) if ver else ""
        got = [(r, _roi(r)) for r in rs]
        done = [(r, x) for r, x in got if x]
        if not done:
            out.append(f"{lane:24}{mode:6}{tag:>10}{len(rs):>5}{0:>8}")
            continue
        n = len(done)
        traded = sum(x[0] for _, x in done) / n
        held = sum(x[1] for _, x in done) / n
        wins = sum(_won(r) for r, _ in done) / n
        staked = sum((r["stake_usd"] or 0.0) for r, _ in done)
        pnl = sum((r["stake_usd"] or 0.0) * x[0] for r, x in done)
        out.append(f"{lane:24}{mode:6}{tag:>10}{len(rs):>5}{n:>8}{_pct(traded):>11}{_pct(held):>9}{_pct(traded - held):>11}{wins:>6.0%}{staked:>10.2f}{pnl:>+9.2f}")
    return out


def exit_table(store: Any, since: float) -> list[str]:
    rows = store.db.execute("SELECT lane, mode, ask, side, outcome, exit_price, exit_reason FROM decisions WHERE action = 'bet' AND ts >= ? AND exit_price IS NOT NULL", (since,)).fetchall()
    by: dict[str, list] = defaultdict(list)
    for r in rows:
        x = _roi(r)
        if x:
            by[_kind(r["exit_reason"])].append(x)
    out = [f"{'exit type':16}{'sales':>6}{'as sold':>10}{'if held':>10}{'saved/cost':>12}   (every lane, live and paper)"]
    for kind, xs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        a, b = sum(x[0] for x in xs) / len(xs), sum(x[1] for x in xs) / len(xs)
        out.append(f"{kind:16}{len(xs):>6}{_pct(a):>10}{_pct(b):>10}{_pct(a - b):>12}")
    return out if len(out) > 1 else ["no exits in the period"]


def calibration_table(store: Any, s: Settings) -> list[str]:
    out = [f"{'lane':24}{'settled':>8}{'skill':>8}{'weight on its estimate':>24}   (skill 0 or less: the estimate is no better than the ask)"]
    for spec in lanes.enabled_lanes(s):
        cal = store.calibrator.adjust(spec.name, "", 0.5, 0.5, spec.trust)
        if cal.n:
            out.append(f"{spec.name:24}{cal.n:>8}{cal.skill if cal.skill is not None else float('nan'):>+8.2f}{cal.weight:>24.2f}")
    return out


def health(s: Settings, store: Any, tape_db: Any, now: float) -> list[str]:
    out = []

    def age(sql: str, table: str) -> str:
        try:
            v = tape_db.execute(sql).fetchone()[0] if table == "tape" else store.db.execute(sql).fetchone()[0]
        except Exception as exc:
            return f"unreadable ({exc})"[:60]
        return "never" if v is None else f"{(now - float(v)) / 60:.0f} min ago"

    out.append(f"newest lane decision: {age('SELECT MAX(ts) FROM decisions', 'main')} | newest Kalshi quote on the tape: {age('SELECT MAX(ts) FROM quotes', 'tape')} | newest futures-flow row: {age('SELECT MAX(ts) FROM fut_1s', 'tape')}")
    try:
        days = tape_db.execute("SELECT COUNT(DISTINCT CAST(ts / 86400 AS INTEGER)) FROM fut_1s").fetchone()[0]
        rows = tape_db.execute("SELECT COUNT(*) FROM fut_1s").fetchone()[0]
        out.append(f"futures flow recorded: {rows:,} rows over {days} UTC days. The test against Kalshi's quotes needs about 14 (the file DATA_DIR/review-notes.md says where that stands)")
    except Exception:
        out.append("futures flow: table not readable")
    for table in ("quotes", "flow_1s", "index_ticks", "feed_ticks", "fut_1s"):
        with contextlib.suppress(Exception):
            out.append(f"  tape.{table}: {tape_db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:,} local rows")
    if db.use_postgres():
        with contextlib.suppress(Exception):
            out.append("database size: " + str(store.db.execute("SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]))
    free = shutil.disk_usage(s.data_dir)
    out.append(f"disk free where the data lives: {free.free / 1e9:.0f} GB of {free.total / 1e9:.0f} GB")
    if archive.enabled(s):
        stats = archive.status(store)
        last = store.db.execute("SELECT MAX(uploaded_ts) FROM archive_manifest").fetchone()[0]
        out.append(f"archive on ({s.archive_bucket}); last upload {'never' if last is None else f'{(now - float(last)) / 3600:.0f} h ago'}; delete-local {'ON' if s.archive_delete_local else 'off'}; hot days {s.archive_hot_days}")
        for r in stats:
            out.append(f"  {r['tbl']:12} {r['days']:>3} days archived {r['first_day']}..{r['last_day']}  {int(r['bytes_n']) / 1e6:>7.1f} MB  {int(r['deleted_days'])} deleted locally")
        missing = []
        for table in archive.TABLES:
            for d in archive.complete_days(tape_db, table, datetime.fromtimestamp(now, UTC)):
                if archive.manifest(store, table, d) is None:
                    missing.append(f"{table} {d}")
        out.append("  complete tape days NOT yet archived: " + (", ".join(missing[:8]) + (" ..." if len(missing) > 8 else "") if missing else "none"))
    else:
        out.append("archive: off (ARCHIVE_BUCKET not set); the tape keeps only TAPE_KEEP_DAYS")
    return out


def market_gaps(store: Any, since: float, now: float) -> list[str]:
    """Settled BTC 15-minute windows per UTC day against the 96 a day Kalshi normally lists: a short day means Kalshi had a gap, or our history did."""
    rows = store.db.execute("SELECT close_ts FROM windows WHERE series = 'KXBTC15M' AND close_ts >= ?", (since,)).fetchall()
    per: dict[str, int] = defaultdict(int)
    for r in rows:
        per[datetime.fromtimestamp(float(r[0]), UTC).strftime("%m-%d")] += 1
    today = datetime.fromtimestamp(now, UTC).strftime("%m-%d")
    return ["BTC 15-minute windows per UTC day: " + "  ".join(f"{d}:{n}{'*' if d == today else ''}" for d, n in sorted(per.items())) + "   (96 is a full day; * is today so far)"]


def notes(s: Settings) -> list[str]:
    p = s.data_dir / "review-notes.md"
    return p.read_text().rstrip().splitlines() if p.is_file() else ["(no DATA_DIR/review-notes.md: nothing recorded as open between reviews)"]


def report(s: Settings, store: Any, tape_db: Any, days: float = 7, now: float | None = None) -> list[str]:
    now = now or time.time()
    since = now - days * 86400
    start = datetime.fromtimestamp(since, UTC).strftime("%Y-%m-%d %H:%M")
    out = [f"REVIEW DATA: the last {days} day(s), {start} UTC to now. env={s.env} mode={s.mode}; live lanes: {', '.join(sorted(s.live_lanes or [])) or 'none'}", ""]
    out += ["== EACH LANE'S BETS (settled ones; 'as traded' includes stops and sells, 'if held' rides every bet to settlement; exits cost = traded minus held)", *lane_table(store, since, s)]
    out += ["", "== WHAT THE EXITS DID", *exit_table(store, since)]
    out += ["", "== CALIBRATION (all time, each lane scored on its own first rating of each market)", *calibration_table(store, s)]
    out += ["", "== DATA AND INFRASTRUCTURE", *health(s, store, tape_db, now), *market_gaps(store, since, now)]
    out += ["", "== OPEN ITEMS NOTED BETWEEN REVIEWS", *notes(s)]
    recent = datetime.fromtimestamp(now, UTC) - timedelta(days=days)
    out += ["", f"(report built {datetime.fromtimestamp(now, UTC):%Y-%m-%d %H:%M} UTC; period starts {recent:%Y-%m-%d})"]
    return out
