"""What the market looked like when each decision was made, and which of it went with profit.

At every decision on a 15-minute market the worker saves a snapshot of the entry context next to the decision (decisions.factors, JSON):
how far into the window it is, the spread, the size on the book for and against the side, contracts traded over the last 60 s, how the side's
bid moved over the last 90 s, how wide it swung, and which way the index was moving into the window. The tape keeps only a few days, so this keeps the context of every bet for as long as the
decisions are kept, and the report below reads it back against what the bet earned.

Only factors that earned a place are kept. Tested on the tape and dropped as noise: taker flow over 15 s and 60 s (no pattern across 132,000
entries), the bid's move over 30 s (under 1 standard error), and the ask (the decision already stores it). What stays is either known to matter
(elapsed minute: late entries lose more) or weakly shown and waiting for more bets (book imbalance, activity, the 90 s move and swing).
The index's move over the 15 minutes before the window opened (`prior_15`, in basis points, signed toward the side bought: a rise is positive for YES,
a fall for NO) is kept at the owner's request. On the tape, windows that opened after a rise ended YES 54% against 46% after a fall (a lean of about 4 points
over what the market charged, with an error margin of about 2.5 and fewer independent periods than windows, as the coins move together), and the 30-minute
and 1-hour moves showed nothing. It is here to be tested on the bot's own bets, not because it has been shown to work.
The report says which of them is signal, candidate or noise, so nothing is added to a model on a hunch.

    python -m kalshi_moonshot factors [--lane NAME]

Nothing here places an order, and a failure to take a snapshot never stops a decision.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from . import curve, forecast
from .kalshiws import index_for_series
from .strategy import fee_per_contract as fee

log = logging.getLogger("moonshot")

FEATURES = ("elapsed_min", "spread", "imbalance", "activity_60", "move_90", "range_90", "prior_15")
WINDOW_MINUTES = 15.0


def _side_quote(side: str, bid: float, ask: float) -> tuple[float, float]:
    return (bid, ask) if side == "yes" else (1.0 - ask, 1.0 - bid)


def snapshot(tape: Any, ticker: str, side: str, close_ts: float, now: float | None = None) -> dict[str, float | None]:
    """The entry context for `side` of `ticker` at `now`, from the tape. Empty if the tape has no recent quote for it. Never raises."""
    now = time.time() if now is None else now
    try:
        q = tape.execute(
            "SELECT ts, bid, ask, bid_size, ask_size FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts",
            (ticker, now - 240.0, now),
        ).fetchall()
        if not q or now - q[-1][0] > 60.0:
            return {}
        qts = [r[0] for r in q]
        bids = [_side_quote(side, r[1], r[2])[0] for r in q]
        bid, ask = _side_quote(side, q[-1][1], q[-1][2])
        out: dict[str, float | None] = {
            "elapsed_min": round(WINDOW_MINUTES - (close_ts - now) / 60.0, 2),
            "spread": round(ask - bid, 4),
            "imbalance": None,
        }
        sb, sa = q[-1][3], q[-1][4]
        if sb is not None and sa is not None and sb + sa > 0:
            mine, other = (sb, sa) if side == "yes" else (sa, sb)  # size bidding for our side against the size offered against it
            out["imbalance"] = round((mine - other) / (mine + other), 3)
        i = bisect.bisect_right(qts, now - 90) - 1
        out["move_90"] = round(bid - bids[i], 4) if i >= 0 else None
        recent = [b for t, b in zip(qts, bids, strict=True) if t >= now - 90.0]
        out["range_90"] = round(max(recent) - min(recent), 4) if recent else None
        flow = tape.execute("SELECT ts, yes_vol, no_vol FROM flow_1s WHERE ticker=? AND ts>? AND ts<=?", (ticker, int(now - 60), int(now))).fetchall()
        out["activity_60"] = round(sum(r[1] + r[2] for r in flow), 1)
        out["prior_15"] = _prior_move(tape, ticker, side, close_ts)
        return out
    except Exception as exc:  # the tape is a convenience here: a decision never waits on it
        log.debug("factors: no snapshot for %s (%s)", ticker, exc)
        return {}


def _prior_move(tape: Any, ticker: str, side: str, close_ts: float) -> float | None:
    """The settlement index's move over the 15 minutes before this window opened, in basis points, signed toward `side`. None when the tape has no reading
    for it (not a crypto market, or the index ticks are missing). The window opened 15 minutes before it closes."""
    index = index_for_series(ticker.split("-")[0])
    if index is None:
        return None
    opened = close_ts - WINDOW_MINUTES * 60

    def value(at: float) -> float | None:
        row = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1", (index, at, at - 10.0)).fetchone()
        return float(row[0]) if row and row[0] else None

    now_v, then_v = value(opened), value(opened - WINDOW_MINUTES * 60)
    if now_v is None or then_v is None:
        return None
    return round((now_v / then_v - 1.0) * 1e4 * (1.0 if side == "yes" else -1.0), 1)


def snapshot_full(tape: Any, ticker: str, side: str, close_ts: float, now: float | None = None, strike: float | None = None,
                  weights: forecast.Weights | None = None, table: curve.CurveTable | None = None) -> dict[str, Any]:
    """Every feature we know of for `side` of `ticker` at `now`: the seven of `snapshot`, plus the price and book as they stand, how the ask moved over 30 and 90 seconds, how far the bid dipped,
    how long the ask has been at 70c or more, the book's update rate, and (a 15-minute crypto market, with the weights and the strike) the six index slices, how far the index is past the strike
    in its usual move to the close, and what the forecaster said (blended, regression, recalled). Empty when the tape has no recent quote. Never raises."""
    now = time.time() if now is None else now
    base = snapshot(tape, ticker, side, close_ts, now)
    if not base:
        return {}
    series = ticker.split("-")[0]
    if not series.endswith("15M"):
        base["elapsed_min"] = base["prior_15"] = None  # those two assume a 15-minute window
    out: dict[str, Any] = dict(base)
    try:
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts", (ticker, now - 900.0, now)).fetchall()
        qts = [r[0] for r in q]
        bids = [_side_quote(side, r[1], r[2])[0] for r in q]
        asks = [_side_quote(side, r[1], r[2])[1] for r in q]
        out["bid"], out["ask"] = round(bids[-1], 4), round(asks[-1], 4)
        i30, i90 = bisect.bisect_right(qts, now - 30) - 1, bisect.bisect_right(qts, now - 90) - 1
        out["rise_30"] = round(asks[-1] - asks[i30], 4) if i30 >= 0 else None
        out["rise_90"] = round(asks[-1] - asks[i90], 4) if i90 >= 0 else None
        out["dip_90"] = round(max(0.0, bids[i90] - min(bids[i90:])), 4) if i90 >= 0 else None
        first = next((k for k, a in enumerate(asks) if a >= 0.70), None)
        out["held_min"] = round((now - qts[first]) / 60.0, 2) if first is not None else None
        out["updates_60"] = sum(1 for x in qts if x >= now - 60.0)
    except Exception as exc:
        log.debug("factors: no extended snapshot for %s (%s)", ticker, exc)
    index_id = index_for_series(series)
    if weights is not None and index_id is not None:
        try:
            moves = forecast.slice_moves(tape, weights, index_id, side, now)
            if moves is not None:
                move, reg, memory = forecast.forecast_move(weights, moves)
                out["slices"] = [round(z, 3) for z in moves]
                out["fc_move"], out["fc_regression"] = round(move, 5), round(reg, 5)
                out["fc_recalled"] = None if memory is None else round(memory, 5)
                vol = weights.sigma.get(index_id, {}).get(3)
                value = forecast._value(tape, index_id, now)
                if strike and vol and value:
                    out["strike"] = strike
                    left = max(0.5, (close_ts - now) / 60.0)
                    out["dist"] = round((1.0 if side == "yes" else -1.0) * math.log(value / strike) / ((vol / math.sqrt(3.0)) * math.sqrt(left)), 3)
        except Exception as exc:
            log.debug("factors: no forecast features for %s (%s)", ticker, exc)
    if table is not None and index_id is not None:
        try:
            path, before, open_value = curve.minute_closes(tape, index_id, close_ts - 900.0, now)
            vol = curve.per_minute_vol(before)
            info = curve.classify(open_value, path, vol) if open_value and vol else None
            if info is not None:
                p_up, base_p, n = table.chance_up(series, info)
                flip = 1.0 if side == "yes" else -1.0
                out["curve_class"], out["curve_minute"], out["curve_windows"] = info["class"], info["minute"], n
                out["curve_range_pos"], out["curve_since_high"] = round(info["range_pos"], 3), info["since_high"]
                out["curve_p"] = round(p_up if flip > 0 else 1 - p_up, 4)  # the table's chance for THIS side
                out["curve_baseline"] = round(base_p if flip > 0 else 1 - base_p, 4)
        except Exception as exc:
            log.debug("factors: no curve features for %s (%s)", ticker, exc)
    return out


def held_minutes(tape: Any, ticker: str, side: str, now: float) -> float | None:
    """Minutes since the side's ask first reached 70c in the last 15 minutes, or None if it has not."""
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts", (ticker, now - 900.0, now)).fetchall()
    first = next((r[0] for r in q if _side_quote(side, r[1], r[2])[1] >= 0.70), None)
    return None if first is None else round((now - first) / 60.0, 2)


_local = threading.local()


def snapshot_now(s: Any, ticker: str, side: str, close_time: str, entry_factors: str = "", strike: float | None = None) -> str:
    """JSON of `snapshot_full` as the market stands now, for a buy or a sale. `entry_factors` is the buy's own snapshot, used to carry the strike over to the sale. Empty on any failure."""
    try:
        close_ts = datetime.fromisoformat(str(close_time)).timestamp()
        if strike is None and entry_factors:
            strike = json.loads(entry_factors).get("strike")
        if getattr(_local, "tape", None) is None:
            from . import db

            _local.tape = db.connect(s.tape_path, "tape", shared=True)
        weights = None
        try:
            weights = forecast.load_weights(s.forecast_weights or f"{s.data_dir}/forecast_h5.json")
        except Exception:
            weights = None  # no weights file: the other features are still worth saving
        snap = snapshot_full(_local.tape, ticker, side, close_ts, None, strike, weights, curve.load_table(s.data_dir))
        return json.dumps(snap) if snap else ""
    except Exception as exc:
        _local.tape = None
        log.debug("factors: no snapshot for %s (%s)", ticker, exc)
        return ""


def snapshot_json(tape: Any, ticker: str, side: str, close_time: str, now: float | None = None) -> str:
    try:
        close_ts = datetime.fromisoformat(close_time).timestamp()
    except (TypeError, ValueError, AttributeError):
        return ""
    snap = snapshot(tape, ticker, side, close_ts, now)
    return json.dumps(snap) if snap else ""


def bet_roi(ask: float, side: str, outcome: str | None, exit_price: float | None) -> float | None:
    """Return per dollar after fees: sold at exit_price (paying the sale fee too), else held to the result (no fee at settlement). None while still open."""
    cost = ask + fee(ask)
    if cost <= 0:
        return None
    if exit_price is not None:
        return (exit_price - fee(exit_price)) / cost - 1.0
    if outcome in {"yes", "no"}:
        return (1.0 - cost) / cost if outcome == (side or "yes") else -1.0
    return None


def _mean_se(groups: dict[Any, list[float]]) -> tuple[float, float, int]:
    allv = [v for g in groups.values() for v in g]
    means = [sum(g) / len(g) for g in groups.values()]
    mean = sum(allv) / len(allv)
    if len(means) < 2:
        return mean, float("nan"), len(allv)
    mm = sum(means) / len(means)
    se = math.sqrt(sum((x - mm) ** 2 for x in means) / (len(means) - 1) / len(means))
    return mean, se, len(allv)


def verdict(ends: dict[str, tuple[float, float]]) -> str:
    """High against low, in standard errors: under 2 is noise, 2 to 3 a candidate to keep watching, 3 or more a signal (still to be confirmed on later bets).
    Several factors are read at once, so a lone 2 is what luck alone produces now and then."""
    if "low" not in ends or "high" not in ends or any(math.isnan(v[1]) for v in (ends["low"], ends["high"])):
        return "too little to judge"
    gap = ends["high"][0] - ends["low"][0]
    z = gap / math.hypot(ends["low"][1], ends["high"][1])
    label = "signal" if abs(z) >= 3 else "candidate" if abs(z) >= 2 else "noise"
    return f"high minus low {gap:+.1%} = {z:+.1f} standard errors: {label}"


def report(store: Any, lane: str | None = None, min_n: int = 30) -> list[str]:
    """Return per factor, in thirds of that factor, for the finished bets that have a snapshot. Standard errors are taken across markets."""
    rows = store.db.execute(
        "SELECT ticker, lane, side, ask, outcome, exit_price, factors FROM decisions WHERE action='bet' AND factors IS NOT NULL AND factors <> '' AND ask IS NOT NULL"
        + (" AND lane=?" if lane else ""),
        ((lane,) if lane else ()),
    ).fetchall()
    data: list[tuple[str, dict, float]] = []
    for r in rows:
        roi = bet_roi(float(r["ask"]), r["side"] or "yes", r["outcome"], r["exit_price"])
        if roi is None:
            continue
        try:
            data.append((r["ticker"], json.loads(r["factors"]), roi))
        except ValueError:
            continue
    name = f"lane {lane}" if lane else "all lanes"
    if len(data) < min_n:
        return [f"{len(data)} finished bets with an entry snapshot for {name}: too few to read (need {min_n}). Snapshots are saved on new decisions only."]
    mean = sum(d[2] for d in data) / len(data)
    out = [f"{len(data)} finished bets with an entry snapshot, {name}: average return {mean:+.1%} per $ after fees", ""]
    for feat in FEATURES:
        vals = sorted(d[1][feat] for d in data if d[1].get(feat) is not None)
        if len(vals) < max(min_n, 9):
            continue
        lo, hi = vals[len(vals) // 3], vals[2 * len(vals) // 3]
        out.append(f"{feat}  (thirds split at {lo:+.3g} and {hi:+.3g})")
        ends: dict[str, tuple[float, float]] = {}
        for label in ("low", "middle", "high"):
            groups: dict[str, list[float]] = defaultdict(list)
            for tk, f, roi in data:
                v = f.get(feat)
                if v is None:
                    continue
                if (label == "low" and v <= lo) or (label == "middle" and lo < v <= hi) or (label == "high" and v > hi):
                    groups[tk].append(roi)
            if groups:
                m, se, n = _mean_se(groups)
                ends[label] = (m, se)
                out.append(f"  {label:7}{n:7} bets{m:+9.1%}  ± {se * 100:.1f}%" if not math.isnan(se) else f"  {label:7}{n:7} bets{m:+9.1%}")
        out.append(f"  -> {verdict(ends)}")
        out.append("")
    return out


def backfill_entry(store: Any, tape: Any, ticker: str) -> int:
    """A buy you made before it could be logged live: build its entry row from the synced account fills and the tape. Returns rows added."""
    fills = store.db.execute(
        "SELECT fill_id, ts, yes_price, no_price, count, order_id, action, side, book_side FROM fills WHERE ticker=? ORDER BY ts, fill_id", (ticker,)
    ).fetchall()
    first = next((f for f in fills if (f["action"] or "buy") == "buy" and not store.is_bot_order(f["order_id"] or "")), None)
    if first is None:
        return 0
    side = first["side"] if first["side"] in {"yes", "no"} else ("yes" if first["book_side"] == "bid" else "no")
    yes_price = first["yes_price"]
    paid = yes_price if side == "yes" else (first["no_price"] if first["no_price"] is not None else 1.0 - yes_price)
    row = store.db.execute("SELECT close_ts FROM market_info WHERE ticker=?", (ticker,)).fetchone()
    close_ts = row["close_ts"] if row else None
    snap = snapshot(tape, ticker, side, float(close_ts), float(first["ts"])) if close_ts and tape is not None else {}
    store.add_manual_entry(trade_id=first["fill_id"], ts=float(first["ts"]), ticker=ticker, side=side, price=paid, count=first["count"],
                           order_id=first["order_id"], close_ts=close_ts, factors=json.dumps(snap) if snap else "")
    return 1


def mark_template(store: Any, tape: Any, ticker: str, note: str = "", undo: bool = False) -> str:
    """Point out one of your buys as a good example. If it was not logged live, it is rebuilt from the account history (run `sync` first) and the tape."""
    changed = store.mark_template(ticker, note, undo)
    if changed or undo:
        return f"{'unmarked' if undo else 'marked'} {changed} buy(s) on {ticker}"
    if backfill_entry(store, tape, ticker):
        store.mark_template(ticker, note)
        return f"{ticker}: rebuilt from your account history and marked as a template"
    return f"{ticker}: no hand-placed buy found. Run `sync` to read the account history, and check the ticker."


def manual_report(store: Any) -> list[str]:
    """The buys you marked as templates, with the market's context at each fill. The rest are only counted: your poor bets teach nothing."""
    rows = store.db.execute("SELECT ts, ticker, side, price, factors, template, note FROM manual_entries ORDER BY ts").fetchall()
    if not rows:
        return ["no hand-placed buys logged yet: they are saved from the live socket as you place them (MANUAL_EXIT must be on)."]
    temps = [r for r in rows if r["template"]]
    lines = [f"{len(rows)} hand-placed buys logged, {len(temps)} marked as templates (mark one with `template TICKER`)"]
    if not temps:
        return lines
    snaps = []
    for r in temps:
        try:
            f = json.loads(r["factors"]) if r["factors"] else {}
        except ValueError:
            f = {}
        snaps.append(f)
        def g(k: str, fmt: str, f: dict = f) -> str:
            return format(f[k], fmt) if f.get(k) is not None else "  -  "

        lines.append(f"  {r['ticker']:34}{r['side']:4} paid {r['price'] if r['price'] is not None else float('nan'):.2f}  min {g('elapsed_min', '5.1f')}  spread {g('spread', '.2f')}  "
                     f"imbalance {g('imbalance', '+.2f')}  moved90 {g('move_90', '+.2f')}  swing {g('range_90', '.2f')}  activity {g('activity_60', '.0f')}  {r['note'] or ''}")
    if len([s for s in snaps if s]) >= 3:
        lines += ["", "what your templates have in common (min / median / max)"]
        for feat in FEATURES:
            v = sorted(s[feat] for s in snaps if s.get(feat) is not None)
            if len(v) >= 3:
                lines.append(f"  {feat:13}{v[0]:+9.3g}{v[len(v) // 2]:+9.3g}{v[-1]:+9.3g}   (n={len(v)})")
        lines.append("  Next step, once there are enough: what share of ordinary entries meets this profile, and how did those do on the tape?")
    return lines
