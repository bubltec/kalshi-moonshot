"""Metrics for the websites: `public.json` (counts only, no money) and `admin.json` (dollars and exit detail).

    python -m kalshi_moonshot.exporter --out site-data

Both files are built from the `decisions` table by the two functions below. The public file is assembled from an explicit allowlist of fields (PUBLIC_LANE_KEYS and
the top-level keys in PUBLIC_KEYS), never by dropping columns from a wider record, so a column added later cannot reach it by accident; tests/test_exporter.py checks that no
money-like key or value appears in it. It carries no tickers, prices, stakes, contract counts or times finer than a day, and leaves out the last DELAY_SECONDS of bets.
A lane's win rate stays hidden (null) until it has MIN_SETTLED settled bets, because a rate over a handful of bets says nothing.
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
PUBLIC_LANE_KEYS = ("lane", "mode", "bets", "settled", "pending", "held_won", "held_lost", "cashed_out", "win_rate", "markets")
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


def build_public(rows: list[Any], now: float) -> dict[str, Any]:
    cutoff = now - DELAY_SECONDS
    by: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for r in rows:
        if r["ts"] <= cutoff:
            by[(r["lane"] or "moonshot", "live" if r["mode"] == "trade" else "paper")].append(r)
    lanes = []
    for (lane, mode), rs in sorted(by.items(), key=lambda kv: (kv[0][1] != "live", kv[0][0])):
        settled = [(r, p) for r in rs if (p := _profit(r)) is not None]
        exited = [(r, p) for r, p in settled if r["exit_price"] is not None]
        held = [(r, p) for r, p in settled if r["exit_price"] is None]
        wins = sum(1 for _, p in settled if p > 0)
        markets: dict[str, int] = defaultdict(int)
        for r in rs:
            markets[series_of(r["ticker"])] += 1
        lanes.append({
            "lane": lane, "mode": mode, "bets": len(rs), "settled": len(settled), "pending": len(rs) - len(settled),
            "held_won": sum(1 for r, _ in held if _won(r) == 1.0), "held_lost": sum(1 for r, _ in held if _won(r) == 0.0),
            "cashed_out": len(exited),
            "win_rate": round(wins / len(settled), 3) if len(settled) >= MIN_SETTLED else None,
            "markets": dict(sorted(markets.items())),
        })
    return {"as_of": _day(cutoff), "min_settled_for_rate": MIN_SETTLED, "lanes": lanes}


def build_admin(rows: list[Any], now: float) -> dict[str, Any]:
    by: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for r in rows:
        by[(r["lane"] or "moonshot", "live" if r["mode"] == "trade" else "paper")].append(r)
    lanes = []
    for (lane, mode), rs in sorted(by.items(), key=lambda kv: (kv[0][1] != "live", kv[0][0])):
        settled = [(r, p) for r in rs if (p := _profit(r)) is not None]
        daily: dict[str, float] = defaultdict(float)
        kinds: dict[str, dict[str, float]] = defaultdict(lambda: {"sales": 0, "pnl_usd": 0.0})
        for r, p in settled:
            pnl = (r["stake_usd"] or 0.0) * p
            daily[_day(r["settled_ts"] or r["ts"])] += pnl
            if r["exit_price"] is not None:
                k = kinds[exit_kind(r["exit_reason"])]
                k["sales"] += 1
                k["pnl_usd"] += pnl
        lanes.append({
            "lane": lane, "mode": mode, "bets": len(rs), "settled": len(settled),
            "win_rate": round(sum(1 for _, p in settled if p > 0) / len(settled), 3) if settled else None,
            "staked_usd": round(sum((r["stake_usd"] or 0.0) for r, _ in settled), 2),
            "pnl_usd": round(sum((r["stake_usd"] or 0.0) * p for r, p in settled), 2),
            "daily_pnl_usd": {d: round(v, 2) for d, v in sorted(daily.items())},
            "exits": {k: {"sales": int(v["sales"]), "pnl_usd": round(v["pnl_usd"], 2)} for k, v in sorted(kinds.items())},
        })
    return {"generated": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"), "lanes": lanes}


SQL = "SELECT ts, ticker, lane, mode, ask, side, outcome, exit_price, exit_reason, stake_usd, settled_ts FROM decisions WHERE action = 'bet'"


def export(store: Any, out: Path, now: float | None = None) -> dict[str, Path]:
    now = time.time() if now is None else now
    rows = store.db.execute(SQL).fetchall()
    out.mkdir(parents=True, exist_ok=True)
    paths = {"public": out / "public.json", "admin": out / "admin.json"}
    paths["public"].write_text(json.dumps(build_public(rows, now), indent=1))
    paths["admin"].write_text(json.dumps(build_admin(rows, now), indent=1))
    return paths


def main() -> int:
    from . import config
    from .store import Store

    ap = argparse.ArgumentParser(description="Write public.json and admin.json for the websites.")
    ap.add_argument("--out", default="site-data")
    args = ap.parse_args()
    s = config.reload()
    paths = export(Store(s.db_path, s.exclude_versions), Path(args.out))
    for name, p in paths.items():
        print(f"{name}: {p} ({p.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
