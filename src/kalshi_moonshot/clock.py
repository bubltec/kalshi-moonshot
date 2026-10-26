"""15-minute favorites, sliced by the exchange clock.

The hourly review looks at the last hour of 15-minute bets against the same clock hour over the
last week (and, once enough slices exist, the same weekday-hour over the last month). 15-minute
markets change inside a session; the hour-of-day is the first repeating grain, then the weekday,
then the week. Nothing here changes a setting.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .store import Store

ET = ZoneInfo("America/New_York")
FIFTEEN_LANES = ("commodity", "fx_favorite", "crypto_favorite")
FAMILIES = ("crypto", "fx", "commodity")


def family_of(ticker: str) -> str:
    """crypto / fx / commodity from a 15-minute ticker."""
    ser = ticker.split("-")[0].removesuffix("15M")
    coin = ser.removeprefix("KX")
    if coin in {"BTC", "ETH", "SOL", "XRP", "DOGE"}:
        return "crypto"
    if any(k in coin for k in ("EUR", "GBP", "JPY", "AUD", "CAD")):
        return "fx"
    return "commodity"


def _pnl(row: Any) -> float:
    entry = float(row["ask"] or 0.0)
    if entry <= 0:
        return 0.0
    if row["exit_price"] is not None:
        return float(row["exit_price"]) - entry
    won = row["outcome"] == (row["side"] or "yes")
    return (1.0 - entry) if won else -entry


def _won(row: Any) -> bool:
    return row["outcome"] == (row["side"] or "yes")


def _et(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, UTC).astimezone(ET)


def _fetch(store: Store, since: float) -> list[Any]:
    marks = ",".join("?" * len(FIFTEEN_LANES))
    return store.db.execute(
        f"""SELECT ts, ticker, lane, side, ask, outcome, exit_price FROM decisions
            WHERE action='bet' AND ticker LIKE '%15M%' AND lane IN ({marks})
              AND outcome IN ('yes','no') AND ts>=?""",
        (*FIFTEEN_LANES, since),
    ).fetchall()


def _summary(rows: list[Any]) -> dict[str, float]:
    if not rows:
        return {"n": 0, "won": 0, "win": 0.0, "pnl": 0.0, "ask": 0.0}
    n = len(rows)
    won = sum(1 for r in rows if _won(r))
    return {
        "n": n,
        "won": won,
        "win": won / n,
        "pnl": sum(_pnl(r) for r in rows) / n,
        "ask": sum(float(r["ask"] or 0.0) for r in rows) / n,
    }


def _line(label: str, s: dict[str, float]) -> str:
    if s["n"] <= 0:
        return f"{label}: no settled 15-minute bets"
    return f"{label}: {int(s['n'])} bets, {s['win']:.0%} won, pnl {s['pnl']:+.1%} /contract, avg ask {s['ask']:.2f}"


def snapshot(store: Store, now: float | None = None, hours: float = 1.0) -> dict[str, Any]:
    """Last `hours` of settled 15-minute favorites, plus the 7-day and weekday-hour comparators."""
    now = time_now(now)
    since = now - hours * 3600
    et = _et(now - hours * 1800)  # midpoint of the window: which clock hour this review is about
    hour_et, dow = et.hour, et.weekday()
    week = _fetch(store, now - 8 * 86400)  # a full week of clock hours, including last week's same hour when this review sits at :45
    month = _fetch(store, now - 28 * 86400)
    this = [r for r in week if float(r["ts"]) >= since]
    same_hour = [r for r in week if _et(float(r["ts"])).hour == hour_et and float(r["ts"]) < since]
    same_wd = [r for r in month if _et(float(r["ts"])).hour == hour_et and _et(float(r["ts"])).weekday() == dow and float(r["ts"]) < since]
    by_hour: dict[int, list] = defaultdict(list)
    for r in week:
        by_hour[_et(float(r["ts"])).hour].append(r)
    by_fam: dict[str, list] = defaultdict(list)
    for r in this:
        by_fam[family_of(r["ticker"])].append(r)
    return {
        "now": now, "hour_et": hour_et, "dow": dow, "hours": hours,
        "this": _summary(this),
        "families": {f: _summary(by_fam.get(f, [])) for f in FAMILIES},
        "same_hour_7d": _summary(same_hour),
        "same_weekday_28d": _summary(same_wd),
        "profile_7d": {h: _summary(by_hour[h]) for h in range(24)},
        "this_rows": this,
    }


def time_now(now: float | None) -> float:
    import time
    return time.time() if now is None else now


def record(store: Store, snap: dict[str, Any]) -> None:
    """Keep this hour's 15-minute slice so daily and weekly repeats can be read later."""
    hour_end = snap["now"]
    hour_et, dow = int(snap["hour_et"]), int(snap["dow"])
    rows = [("all", snap["this"])] + [(f, snap["families"][f]) for f in FAMILIES]
    for fam, s in rows:
        if s["n"] <= 0:
            continue
        store.db.execute(
            "INSERT OR REPLACE INTO hour_slices(hour_end, hour_et, dow, family, n, won, pnl, avg_ask) VALUES(?,?,?,?,?,?,?,?)",
            (hour_end, hour_et, dow, fam, int(s["n"]), int(s["won"]), float(s["pnl"]), float(s["ask"])),
        )
    store.db.commit()


def recorded_weekday(store: Store, hour_et: int, dow: int, family: str = "all") -> dict[str, float]:
    """All saved slices for this weekday-hour (the weekly grain, once enough Mondays exist)."""
    rows = store.db.execute(
        "SELECT n, won, pnl FROM hour_slices WHERE hour_et=? AND dow=? AND family=?",
        (hour_et, dow, family),
    ).fetchall()
    if not rows:
        return {"n": 0, "won": 0, "win": 0.0, "pnl": 0.0, "ask": 0.0, "hours": 0}
    n = sum(int(r["n"]) for r in rows)
    won = sum(int(r["won"]) for r in rows)
    pnl = sum(float(r["pnl"]) * int(r["n"]) for r in rows) / n
    return {"n": n, "won": won, "win": won / n, "pnl": pnl, "ask": 0.0, "hours": len(rows)}


def report(store: Store, snap: dict[str, Any] | None = None, now: float | None = None) -> str:
    """Text the hourly review hands the model: this hour of 15-minute markets vs the clock."""
    snap = snap or snapshot(store, now)
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    label = f"{days[snap['dow']]} {snap['hour_et']:02d}:00 ET"
    lines = [
        f"15-MINUTE FAVORITES, last {snap['hours']:g}h (clock hour {label}). Hourly grain first; weekday-hour once n is enough; weekly when slices repeat.",
        _line("this hour", snap["this"]),
    ]
    for f in FAMILIES:
        if snap["families"][f]["n"]:
            lines.append("  " + _line(f, snap["families"][f]))
    lines.append(_line("same clock hour, last 7 days (excluding this hour)", snap["same_hour_7d"]))
    wd = snap["same_weekday_28d"]
    if wd["n"] >= 12:
        lines.append(_line(f"same {days[snap['dow']]} {snap['hour_et']:02d}:00 over 4 weeks", wd))
    rec = recorded_weekday(store, snap["hour_et"], snap["dow"])
    if rec["hours"] >= 3:
        lines.append(_line(f"saved slices for this weekday-hour ({int(rec['hours'])} past hours)", rec))
    lines.append("last 7 days by ET hour (* this hour):")
    for h in range(24):
        s = snap["profile_7d"][h]
        if s["n"] < 8 and h != snap["hour_et"]:
            continue
        if s["n"] <= 0:
            continue
        mark = "*" if h == snap["hour_et"] else " "
        lines.append(f"  {mark}{h:02d}  {int(s['n']):>4} bets  {s['win']:>4.0%}  pnl {s['pnl']:+.1%}")
    return "\n".join(lines)
