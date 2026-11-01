"""Metrics for the websites: `public.json` (counts only, no money) and `admin.json` (dollars and exit detail).

    python -m kalshi_moonshot.exporter --out site-data

Both files are built from the `decisions` table by the two functions below. The public file is assembled from an explicit allowlist of fields (PUBLIC_LANE_KEYS and
the top-level keys in PUBLIC_KEYS), never by dropping columns from a wider record, so a column added later cannot reach it by accident; tests/test_exporter.py checks that no
money-like key or value appears in it. It carries no tickers, prices, stakes, contract counts or times finer than a day (its per-day lists are settled and won counts), and leaves out the last DELAY_SECONDS of bets.
A lane's win rate stays hidden (null) until it has MIN_SETTLED settled bets, because a rate over a handful of bets says nothing.
Only live lanes in the current config are listed publicly; a deleted lane's history stays in admin.json, marked retired.
Read-only: nothing here changes a setting or places an order.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .digest import EXIT_SLIP, _won
from .strategy import fee_per_contract as fee

DELAY_SECONDS = 24 * 3600.0
MIN_SETTLED = 30
PUBLIC_KEYS = ("as_of", "min_settled_for_rate", "lanes")
PUBLIC_LANE_KEYS = ("lane", "mode", "bets", "settled", "pending", "held_won", "held_lost", "cashed_out", "win_rate", "markets", "daily")
EXIT_KINDS = ("stop-loss", "burst", "settlement-guard", "peak-crash", "hover", "unstable", "spike", "take-profit", "strike-through", "cash-out", "drop", "scalp")


def exit_kind(reason: str | None) -> str:
    r = (reason or "").lower()
    for key in EXIT_KINDS:
        if r.startswith(key):
            return key
    return "other exit"


def series_of(ticker: str) -> str:
    """KXBTC15M-26OCT311230-30 -> KXBTC15M: the kind of market, never the specific one."""
    return (ticker or "").split("-")[0]


def _profit(r: Any) -> float | None:
    """Return per $1 staked, after fees, for a settled bet; None if it has no ask or is not settled. The exporter keeps the sign only for the public file."""
    if r["ask"] is None or r["outcome"] not in ("yes", "no"):
        return None
    cost = r["ask"] + fee(r["ask"])
    if r["exit_price"] is not None:
        return (r["exit_price"] - EXIT_SLIP - fee(r["exit_price"])) / cost - 1
    return _won(r) / cost - 1


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def build_public(rows: list[Any], now: float, active: set[str] | None = None) -> dict[str, Any]:
    """`active` is the set of lanes in the current config; a lane that was deleted or retired is not listed. None lists every lane in the rows (tests)."""
    cutoff = now - DELAY_SECONDS
    by: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for r in rows:
        if r["ts"] <= cutoff and r["mode"] == "trade" and (active is None or (r["lane"] or "moonshot") in active):  # only live lanes are public
            by[(r["lane"] or "moonshot", "live" if r["mode"] == "trade" else "paper")].append(r)
    lanes = []
    for (lane, mode), rs in sorted(by.items(), key=lambda kv: (kv[0][1] != "live", kv[0][0])):
        settled = [(r, p) for r in rs if (p := _profit(r)) is not None]
        exited = [(r, p) for r, p in settled if r["exit_price"] is not None]
        held = [(r, p) for r, p in settled if r["exit_price"] is None]
        wins = sum(1 for _, p in settled if p > 0)
        days: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r, p in settled:  # settled bets and wins per day: counts only, nothing finer than a day
            d = days[_day(r["settled_ts"] or r["ts"])]
            d[0] += 1
            d[1] += p > 0
        markets: dict[str, int] = defaultdict(int)
        for r in rs:
            markets[series_of(r["ticker"])] += 1
        lanes.append({
            "lane": lane, "mode": mode, "bets": len(rs), "settled": len(settled), "pending": len(rs) - len(settled),
            "held_won": sum(1 for r, _ in held if _won(r) == 1.0), "held_lost": sum(1 for r, _ in held if _won(r) == 0.0),
            "cashed_out": len(exited),
            "win_rate": round(wins / len(settled), 3) if len(settled) >= MIN_SETTLED else None,
            "markets": dict(sorted(markets.items())),
            "daily": [[d, v[0], v[1]] for d, v in sorted(days.items())],
        })
    return {"as_of": _day(cutoff), "min_settled_for_rate": MIN_SETTLED, "lanes": lanes}


def build_admin(rows: list[Any], now: float, active: set[str] | None = None) -> dict[str, Any]:
    """Every lane that has rows, each marked `retired` when it is no longer in the current config, so a deleted lane's record stays readable here."""
    by: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for r in rows:
        by[(r["lane"] or "moonshot", "live" if r["mode"] == "trade" else "paper")].append(r)
    lanes = []
    for (lane, mode), rs in sorted(by.items(), key=lambda kv: (kv[0][1] != "live", kv[0][0])):
        settled = [(r, p) for r in rs if (p := _profit(r)) is not None]
        daily: dict[str, float] = defaultdict(float)
        days: dict[str, dict[str, float]] = defaultdict(lambda: {"settled": 0, "wins": 0, "staked_usd": 0.0, "pnl_usd": 0.0})
        kinds: dict[str, dict[str, float]] = defaultdict(lambda: {"sales": 0, "pnl_usd": 0.0})
        events = sorted([int(r["settled_ts"] or r["ts"]), round((r["stake_usd"] or 0.0) * p, 2), round(r["stake_usd"] or 0.0, 2), int(p > 0)] for r, p in settled)
        for r, p in settled:
            pnl = (r["stake_usd"] or 0.0) * p
            day = _day(r["settled_ts"] or r["ts"])
            daily[day] += pnl
            d = days[day]
            d["settled"] += 1
            d["wins"] += p > 0
            d["staked_usd"] += r["stake_usd"] or 0.0
            d["pnl_usd"] += pnl
            if r["exit_price"] is not None:
                k = kinds[exit_kind(r["exit_reason"])]
                k["sales"] += 1
                k["pnl_usd"] += pnl
        lanes.append({
            "lane": lane, "mode": mode, "retired": active is not None and lane not in active, "bets": len(rs), "settled": len(settled),
            "win_rate": round(sum(1 for _, p in settled if p > 0) / len(settled), 3) if settled else None,
            "staked_usd": round(sum((r["stake_usd"] or 0.0) for r, _ in settled), 2),
            "pnl_usd": round(sum((r["stake_usd"] or 0.0) * p for r, p in settled), 2),
            "daily_pnl_usd": {d: round(v, 2) for d, v in sorted(daily.items())},
            # the same days with counts, so the page can total every lane over one shared window
            "daily": {d: {"settled": int(v["settled"]), "wins": int(v["wins"]), "staked_usd": round(v["staked_usd"], 2), "pnl_usd": round(v["pnl_usd"], 2)} for d, v in sorted(days.items())},
            "settles": events,  # [settled time, P&L $, stake $, 1 if a win] per settled bet: the page draws curves and totals any window from these
            "last_bet": _day(max(r["ts"] for r in rs)),
            "exits": {k: {"sales": int(v["sales"]), "pnl_usd": round(v["pnl_usd"], 2)} for k, v in sorted(kinds.items())},
        })
    return {"generated": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"), "lanes": lanes}


def validate_public(data: dict[str, Any]) -> None:
    """Raise if a public.json carries anything beyond the allowlist: an unknown key, a dollar sign, or a market that is a specific ticker. Run before every upload."""
    if set(data) != set(PUBLIC_KEYS):
        raise ValueError(f"public.json top-level keys {sorted(data)} are not {sorted(PUBLIC_KEYS)}")
    for lane in data["lanes"]:
        if set(lane) != set(PUBLIC_LANE_KEYS):
            raise ValueError(f"lane {lane.get('lane')!r} has keys {sorted(lane)}")
        if any("-" in series for series in lane["markets"]):
            raise ValueError(f"lane {lane['lane']!r} lists a specific ticker")
    if "$" in json.dumps(data):
        raise ValueError("public.json contains a dollar sign")


SQL = "SELECT ts, ticker, lane, mode, ask, side, outcome, exit_price, exit_reason, stake_usd, settled_ts FROM decisions WHERE action = 'bet'"


def public_json(store: Any, now: float | None = None, active: set[str] | None = None) -> dict[str, Any]:
    """The public file's content, checked against the allowlist before it is returned."""
    data = build_public(store.db.execute(SQL).fetchall(), time.time() if now is None else now, active)
    validate_public(data)
    return data


def admin_json(store: Any, now: float | None = None, active: set[str] | None = None) -> dict[str, Any]:
    return build_admin(store.db.execute(SQL).fetchall(), time.time() if now is None else now, active)


def export(store: Any, out: Path, now: float | None = None, active: set[str] | None = None) -> dict[str, Path]:
    now = time.time() if now is None else now
    rows = store.db.execute(SQL).fetchall()
    out.mkdir(parents=True, exist_ok=True)
    paths = {"public": out / "public.json", "admin": out / "admin.json"}
    paths["public"].write_text(json.dumps(build_public(rows, now, active), indent=1))
    paths["admin"].write_text(json.dumps(build_admin(rows, now, active), indent=1))
    return paths


def main() -> int:
    from . import config, lanes
    from .store import Store

    ap = argparse.ArgumentParser(description="Write public.json and admin.json for the websites.")
    ap.add_argument("--out", default="site-data")
    ap.add_argument("--check", metavar="FILE", help="only validate this public.json against the allowlist (exit 1 if it fails)")
    args = ap.parse_args()
    if args.check:
        validate_public(json.loads(Path(args.check).read_text()))
        print(f"{args.check}: ok")
        return 0
    s = config.reload()
    paths = export(Store(s.db_path, s.exclude_versions), Path(args.out), active=set(lanes.lane_specs(s)))
    for name, p in paths.items():
        print(f"{name}: {p} ({p.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
