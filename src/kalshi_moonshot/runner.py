"""One scan pass over two lanes, then log every decision.

moonshot: cheap long shots, a small chance of a large payout.
steady:   likely outcomes whose ask has been rising, a small payout that repeats.

For each candidate the model gets the question, similar settled cases, and recent news
headlines. Orders are placed only when MODE=trade and the risk gate agrees.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, NamedTuple

from . import factors, gate, risk, shadow, steptime
from . import scale as scale_mod
from .calibration import Calibration
from .commodities import CommodityPricer, load_instruments
from .config import Settings
from .estimators import Estimate, EstimateShare, Estimator, build_estimators
from .kalshi import MARKET_PAGES, KalshiClient, KalshiError, KalshiRateLimited
from .lanes import (
    LADDER,
    LaneSpec,
    chance_ok,
    lane_specs,
    main_lanes,
    settings_of,
    spec_for,
    stop_drop_for,
    takes_profit,
    tiered_lanes,
    version_of,
    wants_bet,
)
from .marketdata import shared_for
from .search import NewsSearch
from .sizing import Sizing, size_bet
from .store import Store
from .strategy import (
    LongShot,
    ask_trend,
    best_ask,
    best_bid,
    cash_if_losing,
    cash_if_up,
    early_cash,
    edge,
    falling_reason,
    flip,
    hard_loss,
    lane_candidates,
    lane_reject_reason,
    one_per_mutex_event,
    order_fee,
    price_shape,
    rising,
    scalp_hit,
    side_ask_notional,
    side_prob,
    stop_loss_hit,
    stop_wait,
    take_profit_hit,
    watch_trend,
)

log = logging.getLogger("moonshot")


@dataclass
class Outcome:
    ticker: str
    title: str
    ask: float
    model_p: float | None
    edge: float | None
    action: str
    note: str
    lane: str = "moonshot"


def candidate_markets(s: Settings, client: KalshiClient, partial_ok: bool = True) -> list[dict]:
    """Open markets closing inside the window either lane is looking at, with category attached.

    Fetched by close time. The plain market list and the event list are not ordered by close
    time, so reading the first few pages can return zero markets inside a short window.

    If a page fails after every retry: partial_ok keeps what was read (with a warning),
    otherwise the error is raised so a queued job can retry the whole fetch later.
    """
    lanes = main_lanes(s)
    lo = min(lane.hours[0] for lane in lanes) if lanes else s.min_hours_to_close
    hi = max(lane.hours[1] for lane in lanes) if lanes else s.max_hours_to_close
    now = time.time()
    info = client.event_info()
    skip = {c.casefold() for c in s.skip_categories}
    out: list[dict] = []
    fetched = 0
    try:
        for m in client.markets_closing(now + lo * 3600, now + hi * 3600):
            fetched += 1
            raw = info.get(m.get("event_ticker", ""), ("", "", False))
            category, series, mutex = (*raw, "", "", False)[:3]
            if category and category.casefold() in skip:
                continue
            m["category"] = category
            m["mutually_exclusive"] = bool(mutex)
            if series:
                m.setdefault("series_ticker", series)
            out.append(m)
    except KalshiError as exc:
        if not partial_ok:
            raise
        log.warning("market fetch stopped after %d markets (%s); using what was read", fetched, exc)
    if fetched >= MARKET_PAGES * getattr(client, "page_size", 1000):
        log.warning(
            "fetched the %d-market limit for a %.0f-%.0fh window; some markets were not read. "
            "Narrow the hour limits.", fetched, lo, hi,
        )
    return out


def _apply_event_info(client: KalshiClient, shots: list[LongShot]) -> list[LongShot]:
    """Fill category / series / mutex from the event, including a fetch for cache misses."""
    if not shots or not hasattr(client, "fill_event_info"):
        return shots
    info = client.fill_event_info([x.event_ticker for x in shots])
    out: list[LongShot] = []
    for shot in shots:
        raw = info.get(shot.event_ticker, ("", "", False))
        category, series, mutex = (*raw, "", "", False)[:3]
        out.append(replace(
            shot,
            category=shot.category or category,
            series=shot.series or series,
            mutually_exclusive=shot.mutually_exclusive or bool(mutex),
        ))
    return out


def lane_shots(
    s: Settings, client: KalshiClient, markets: list[dict], spec: LaneSpec,
    estimators: dict[str, Estimator] | None = None, now: datetime | None = None,
) -> list[LongShot]:
    """One lane's shortlist: its filter, its estimator's screen, its trend rule and one-per rule."""
    now = now or datetime.now(UTC)
    if estimators is not None and spec.estimator not in estimators:
        return []  # nothing to rate this lane with (e.g. no price feed): do not queue dead work
    cands = lane_candidates(markets, spec, s, now)
    estimator = (estimators or {}).get(spec.estimator)
    if estimator is not None:
        cands = estimator.screen(cands, markets, spec, s, now)
    pool = _apply_event_info(client, cands[: spec.shortlist * 3])
    out: list[LongShot] = []
    for shot in pool:
        if spec.trend != "none":
            try:
                candles: list | None = client.candlesticks(shot.series, shot.ticker, spec.lookback_hours)
            except KalshiError:
                candles = None
            t = ask_trend(candles or [], shot.side)
            if spec.trend == "rising":
                if t is None or not rising(t, spec.min_trend):
                    continue
            elif falling_reason(t, spec.max_drop):
                continue
            shot = replace(shot, trend=None if t is None else t.change)
        out.append(shot)
        if spec.one_per != "mutex" and len(out) >= spec.shortlist:
            break
    if spec.one_per == "mutex":
        out = one_per_mutex_event(out)
    elif spec.one_per == "event":
        best: dict[str, LongShot] = {}
        for shot in out:  # input is already best-first
            best.setdefault(shot.event_ticker or shot.ticker, shot)
        out = list(best.values())
    return out[: spec.shortlist]


def shortlists(
    s: Settings, client: KalshiClient, markets: list[dict],
    estimators: dict[str, Estimator] | None = None, now: datetime | None = None,
    lanes: list[LaneSpec] | None = None,
) -> dict[str, list[LongShot]]:
    """Each main-scan lane's candidates (two-level lanes have their own scope/review passes).
    A market goes to the first lane that takes it, and a lane skips events an earlier lane
    already holds a side of."""
    now = now or datetime.now(UTC)
    out: dict[str, list[LongShot]] = {}
    taken: set[str] = set()
    taken_events: set[str] = set()
    for spec in main_lanes(s) if lanes is None else lanes:
        shots = lane_shots(s, client, markets, spec, estimators, now)
        if spec.shares_markets:  # a control lane sees the same markets as the lane it is compared with
            out[spec.name] = shots
            continue
        picked = [
            x for x in shots
            if x.ticker not in taken
            and not (x.mutually_exclusive and x.event_ticker in taken_events)
        ]
        out[spec.name] = picked
        taken.update(x.ticker for x in picked)
        taken_events.update(x.event_ticker for x in picked if x.mutually_exclusive and x.event_ticker)
    return out


def _all_series(spec: LaneSpec, s: Settings) -> list[str]:
    if spec.series:
        return list(spec.series)
    return [
        i.prefix + suffix for i in load_instruments(s.data_dir) for suffix in i.suffixes
    ]


def watched(s: Settings) -> set[str] | None:
    """The series the bot follows when WATCH_SERIES opts in: that list, TAPE_EXTRA_SERIES, and every series a live lane uses (a live lane is never starved). None = no list, follow everything."""
    if not s.watch_series:
        return None
    out = set(s.watch_series) | set(s.tape_extra_series)
    for spec in tiered_lanes(s):
        if s.live_lanes is None or spec.name in s.live_lanes:
            out |= set(_all_series(spec, s))
    return out


def lane_series(spec: LaneSpec, s: Settings) -> list[str]:
    """Series a two-level lane fetches by name: its own list, else every priced commodity series; with WATCH_SERIES set, only those on the list (a live lane keeps all of its own)."""
    names = _all_series(spec, s)
    keep = watched(s)
    return names if keep is None else [n for n in names if n in keep]


def scope_lane(
    s: Settings, client: KalshiClient, store: Store, spec: LaneSpec, now: datetime | None = None,
    quant: CommodityPricer | None = None,
) -> int:
    """Level 1, slow: find the events worth watching and save them as the lane's watchlist.

    Only the lane's own series are fetched (a handful of requests, not the whole exchange). An
    event is kept when one of its markets passes the lane's filter apart from price, spread and
    volume, which move too fast to scope on. Returns the number of events watched.
    """
    now = now or datetime.now(UTC)
    if quant is not None and spec.timed_suffix:
        quant.learn_windows(client, s, now)  # keep each 15-minute series' feed offset fresh
    relaxed = replace(
        spec, price=(0.0, 1.0), max_spread=1.0, min_volume=0.0, min_return=0.0,
        entry_elapsed=(0.0, 1e9),  # a window not yet in its entry range must still be watched
    )
    try:
        info = client.event_info()
    except KalshiError:
        info = {}
    found: dict[str, dict] = {}
    names = lane_series(spec, s)
    failed = 0
    for series in names:
        try:
            markets = client.series_markets(series)
        except KalshiError as exc:
            failed += 1
            log.warning("scope %s: could not read %s (%s)", spec.name, series, exc)
            continue
        for m in markets:
            category = (info.get(m.get("event_ticker", ""), ("",))[0]) or ""
            m["category"] = category
            m.setdefault("series_ticker", series)
            if lane_reject_reason(m, relaxed, s, now) is None:
                found.setdefault(
                    m["event_ticker"],
                    {"close": m.get("close_time", ""), "category": category, "series": series},
                )
    if names and failed == len(names):
        raise KalshiError(f"scope {spec.name}: every series fetch failed")
    soonest = sorted(found.items(), key=lambda kv: kv[1]["close"])[: spec.watch_max]
    store.set_watch(spec.name, dict(soonest))
    return len(soonest)


def review_lane(
    s: Settings, client: KalshiClient, store: Store, spec: LaneSpec,
    estimators: dict[str, Estimator] | None, now: datetime | None = None, only: set[str] | None = None,
) -> list[LongShot]:
    """Level 2, fast: re-read only the watched events and return markets worth evaluating now.
    `only` limits the pass to those events (the socket entry watcher reviews just the one that moved).

    Markets decided on within the lane's cooldown are dropped, so a two-minute loop does not
    log the same unchanged view thirty times an hour.
    """
    now = now or datetime.now(UTC)
    markets: list[dict] = []
    for event, info in sorted(store.watch(spec.name).items(), key=lambda kv: kv[1].get("close", "")):
        if only is not None and event not in only:
            continue
        close = _hours_left(info.get("close", ""), now)
        if close is not None and close <= 0:
            store.unwatch(spec.name, event)
            continue
        try:
            rows = client.event_markets(event)
        except KalshiError:
            continue
        for m in rows:
            m["category"] = info.get("category", "")
            m.setdefault("series_ticker", info.get("series", ""))
        markets.extend(rows)
    if spec.shadow_suffix:
        ladder = (estimators or {}).get(LADDER)
        shadow.snapshot(store, s, markets, spec.shadow_suffix, getattr(ladder, "pricer", None), now)
    wide = replace(spec, shortlist=spec.shortlist * 3)
    shots = lane_shots(s, client, markets, wide, estimators, now)
    dropped = getattr((estimators or {}).get(spec.estimator), "dropped", None)
    if dropped:
        store.log_screened(spec.name, dropped, now.timestamp())
    if spec.cooldown_minutes:
        # A short market must be re-read as it nears the close: its outcome is only decided late.
        # So the cooldown is at most a fifth of the time left.
        def cooling(x: LongShot) -> bool:
            minutes = min(spec.cooldown_minutes, max(0.5, x.hours_to_close * 60.0 / 5.0))
            return store.decided_since(x.ticker, now.timestamp() - minutes * 60)

        shots = [x for x in shots if not cooling(x)]
    return shots[: spec.shortlist]


def moonshot_shots(markets, s, client, now=None) -> list[LongShot]:
    return lane_shots(s, client, markets, spec_for(s, "moonshot"), None, now)


def steady_shots(markets, s, client, now=None) -> list[LongShot]:
    return lane_shots(s, client, markets, spec_for(s, "steady"), None, now)


def shortlist(
    s: Settings, client: KalshiClient, markets: list[dict], now: datetime | None = None
) -> tuple[list[LongShot], list[LongShot]]:
    """(moonshot, steady) candidates, from an already-fetched market list."""
    lanes = shortlists(s, client, markets, None, now)
    return lanes.get("moonshot", []), lanes.get("steady", [])


def describe_scan(s: Settings, total: int, counts: dict[str, int], searcher: object | None) -> str:
    skipped = ", ".join(sorted(s.skip_categories))
    lanes = ", ".join(f"{n} {name}" for name, n in counts.items())
    return (
        f"{total} open markets: {lanes}"
        f"{f', skipping {skipped}' if skipped else ''}"
        f"{', news on' if searcher else ', news off'}"
    )


def format_outcome(r: Outcome) -> str:
    p = f"{r.model_p:.3f}" if r.model_p is not None else "  -  "
    e = f"{r.edge:+.3f}" if r.edge is not None else "  -  "
    return (
        f"{r.action:4} {r.lane[:4]:4} ask={r.ask:.2f} model={p} edge={e}  {r.ticker}\n"
        f"     {r.title[:90]} | {r.note[:110]}"
    )


def scan(
    s: Settings, client: KalshiClient, est, store: Store,
    searcher: NewsSearch | None = None, quant: CommodityPricer | None = None,
) -> list[Outcome]:
    """One synchronous pass (the `scan` command). The `run` loop uses the job queue instead."""
    estimators = build_estimators(est, store, searcher, quant)
    markets = candidate_markets(s, client)
    lanes = shortlists(s, client, markets, estimators)
    for spec in tiered_lanes(s):  # one-off scan: do both levels back to back
        scope_lane(s, client, store, spec, quant=quant)
        lanes[spec.name] = review_lane(s, client, store, spec, estimators)
    log.info(describe_scan(s, len(markets), {k: len(v) for k, v in lanes.items()}, searcher))
    return [
        evaluate(s, client, est, store, shot, searcher, quant=quant, estimators=estimators)
        for shots in lanes.values() for shot in shots
    ]


_NOTED: set[tuple[str, str, str]] = set()  # lane versions already recorded in this process


def _lane_version(store: Store, s: Settings, lane: str) -> str | None:
    """The version of the lane's logic in force now; the first use of a new one is recorded with its settings."""
    spec = lane_specs(s).get(lane)
    if spec is None:
        return None
    ver = version_of(spec, s)
    key = (str(store.path), lane, ver)
    if key not in _NOTED:
        store.note_lane_version(lane, ver, settings_of(spec, s))
        _NOTED.add(key)
    return ver


def _log(store: Store, s: Settings, shot: LongShot, **fields) -> None:
    fields.setdefault("lane_ver", _lane_version(store, s, shot.lane))
    if shot.factors:
        fields.setdefault("factors", shot.factors)
    store.add_decision(
        ticker=shot.ticker, title=shot.title, bid=shot.bid, ask=shot.ask, mode="trade" if s.is_live(shot.lane, shot.series) else "observe",
        close_time=shot.close_time, lane=shot.lane, category=shot.category,
        event_ticker=shot.event_ticker, **fields,
    )


def _knn_says(s: Settings, client: KalshiClient, store: Store, spec: LaneSpec, shot: LongShot) -> tuple[bool, str]:
    """Ask the nearest-neighbour model (knn.py) whether entries like this one have paid."""
    from . import knn

    try:
        strike = float(client.market(shot.ticker).get("floor_strike"))
    except (TypeError, ValueError, KalshiError):
        strike = None
    try:
        close = datetime.fromisoformat(shot.close_time).timestamp()
    except (TypeError, ValueError):
        return False, "knn: no close time"
    return knn.says(store, spec, s.tape_path, shot.ticker, shot.series, shot.side, time.time(), close, strike, shot.ask, gate.since_ts(spec.gate_since))


def _grid_says(s: Settings, client: KalshiClient, store: Store, spec: LaneSpec, shot: LongShot) -> tuple[bool, str]:
    """Ask the learned grid (grid.py) whether the conditions right now are ones that have paid: minute, price, last hour, whole market."""
    from . import grid

    tape = s.tape_path
    series = tuple(spec.series) or (shot.series,)
    grid.refresh_soon(lambda: Store(s.db_path, s.exclude_versions), tape, series, spec.grid_rule)
    elapsed = spec.timed_minutes - shot.hours_to_close * 60.0
    try:
        strike = float(client.market(shot.ticker).get("floor_strike"))
    except (TypeError, ValueError, KalshiError):
        strike = None
    edge, alpha, beta = grid.live_features(tape, shot.series, shot.side, time.time(), strike, shot.hours_to_close * 60.0, shot.ask)
    go, why = grid.pick(store, spec, series, elapsed, shot.ask, edge, alpha, beta, gate.since_ts(spec.gate_since))
    return go, f"{why} [price {edge or '?'}, hour {alpha or '?'}, market {beta or '?'}]"


def evaluate(
    s: Settings, client: KalshiClient, est, store: Store, shot: LongShot,
    searcher: NewsSearch | None = None,
    *,
    quant: CommodityPricer | None = None,
    estimators: dict[str, Estimator] | None = None,
    raise_model_errors: bool = False,
    order_key: str | None = None,
    before_order: Callable[[], None] | None = None,
    share: EstimateShare | None = None,
) -> Outcome:
    """Rate one market, log the decision, and place an order if allowed.

    The lane's spec names the estimator and the bet rule. The estimate is then weighed against
    our own settled record (calibration.py) before the rule is applied.

    raise_model_errors: let an estimator failure propagate (so a queued job can retry it) instead
    of logging a "model error" pass. order_key makes the order id stable across retries.
    before_order runs just before an order is sent, so the caller can mark that moment.
    share: reuse another lane's recent estimate of this market instead of asking again (estimators.EstimateShare).
    """
    spec = spec_for(s, shot.lane)
    estimators = estimators or build_estimators(est, store, searcher, quant)
    try:
        estimator = estimators.get(spec.estimator)
        if estimator is None:
            raise RuntimeError(f"no {spec.estimator!r} estimator available for lane {spec.name}")
        if share is not None:
            got: Estimate = share.get(spec.estimator, shot, s.estimate_share_seconds, lambda: estimator.estimate(client, s, shot))
        else:
            got = estimator.estimate(client, s, shot)
    except Exception as exc:  # model down, bad JSON, timeout: skip, never trade blind
        if raise_model_errors:
            raise
        _log(
            store, s, shot, model_p=None, edge=None, action="pass",
            reason=f"model error: {exc}"[:200], context=getattr(exc, "context", "")[:1500],
        )
        return Outcome(
            shot.ticker, shot.title, shot.ask, None, None, "pass", f"model error: {exc}", shot.lane
        )

    p, why = side_prob(got.p, shot.side), got.why  # chance the contract we would buy pays
    cal: Calibration = store.calibrator.adjust(shot.lane, shot.category, shot.ask, p, spec.trust)
    e = edge(cal.p, shot.ask)
    bet = wants_bet(spec, cal.p, shot.ask, e)
    note = why if cal.n == 0 else f"{why} | {cal.note(p)}"
    if bet and spec.gate_from:
        open_, gate_note = gate.check(store, spec, shot.close_time, series=shot.series)
        if not open_:
            bet = False
            note = f"{gate_note}. {note}"
    if bet and spec.knn:
        go, knote = _knn_says(s, client, store, spec, shot)
        note = f"{knote}. {note}"
        bet = go
    elif bet and spec.grid:
        go, gnote = _grid_says(s, client, store, spec, shot)
        note = f"{gnote}. {note}"
        bet = go
    if bet and not chance_ok(spec, p):
        bet = False
        note = f"chance {p:.0%} outside the lane's {spec.min_chance:.0%}-{spec.max_chance:.0%}. {note}"
    if bet and shot.mutually_exclusive:
        others = store.bet_tickers_in_event(shot.event_ticker) - {shot.ticker}
        if others:
            bet = False
            note = f"other side already taken ({min(others)}). {note}"
    if bet and spec.reentry_cooldown_seconds and (left := store.exited_within(shot.ticker, shot.lane, spec.reentry_cooldown_seconds)) > 0:
        bet = False
        note = f"cooling down: this lane sold {shot.ticker} {spec.reentry_cooldown_seconds - left:.0f}s ago and waits {spec.reentry_cooldown_seconds:.0f}s before buying it again. {note}"
    if bet and spec.max_open > 0:
        open_now = sum(1 for r in store.open_bets() if (r["lane"] or "moonshot") == shot.lane and r["ticker"] != shot.ticker)
        if open_now >= spec.max_open:
            bet = False
            note = f"lane full: {open_now} bets already open (the lane holds at most {spec.max_open}). {note}"
    if bet and spec.min_dwell_seconds > 0:
        dwell = dwell_seconds(s, shot, spec.price[0])
        if dwell is not None and dwell < spec.min_dwell_seconds:
            bet = False
            note = f"just arrived: this side has been in the price band {dwell:.0f}s and the lane waits {spec.min_dwell_seconds:.0f}s. {note}"
    if bet and spec.entry_trend_seconds > 0 and spec.entry_trend_z is not None:
        zed = side_trend_z(s, shot, spec.entry_trend_seconds)
        if zed is not None and zed <= spec.entry_trend_z:
            bet = False
            note = f"trending against: this side moved {zed:+.1f} typical moves over the last {spec.entry_trend_seconds:.0f}s and the lane needs more than {spec.entry_trend_z:+.1f}. {note}"
    elif bet and spec.entry_trend_seconds > 0:
        moved = side_trend(s, shot, spec.entry_trend_seconds)
        if moved is not None and moved < spec.entry_trend_min:
            bet = False
            note = f"trending against: this side's price moved {100 * moved:+.1f}c over the last {spec.entry_trend_seconds:.0f}s and the lane needs {100 * spec.entry_trend_min:+.1f}c. {note}"
    if bet and spec.entry_spot_ticks > 0:
        aligned = spot_ticks_aligned(s, shot, spec.entry_spot_ticks)
        if aligned is False:
            bet = False
            note = f"against the coin: its last {spec.entry_spot_ticks} spot price changes did not all go {'up' if shot.side == 'yes' else 'down'}. {note}"
    if bet and spec.entry_spot_seconds > 0:
        moved_spot = spot_move(s, shot, spec.entry_spot_seconds)
        if moved_spot is not None and moved_spot <= 0:
            bet = False
            note = f"against the coin: its price moved {'down' if shot.side == 'yes' else 'up'} or stood still over the last {spec.entry_spot_seconds:.0f}s. {note}"
    sized = Sizing(0, 0.0, "")
    if bet:
        with steptime.step("balance read"):
            balance = store.bankroll.read(client, s, live=s.is_live(shot.lane, shot.series))
        grown = scale_mod.for_lane(store, spec, balance.equity, s=s)
        sized = size_bet(
            spec, s, cal.p, shot.ask, balance.available, balance.equity,
            balance.open_by_lane.get(shot.lane, 0.0), grown.mult, fractional=s.fractional_stake and not _NO_FRACTION,
            maker_price=round(max(0.01, shot.ask - spec.buy_rest_below), 4) if spec.buy_rest_seconds > 0 else None,
        )
        if grown.note:
            note = f"{grown.note}. {note}"
        if store.holds_sized_bet(shot.ticker, shot.lane) and not s.is_live(shot.lane, shot.series):
            sized = Sizing(0, 0.0, "already holding this market (paper)")
        elif not sized.ok:
            bet = False
            note = f"not sized: {sized.why}. {note}"
        else:
            note = f"{sized.why}. {note}"
    if bet and s.is_live(shot.lane, shot.series):
        with contextlib.nullcontext() if spec.buy_rest_seconds > 0 else _ORDER_LOCK:  # a resting buy waits for its fill: it must not hold every other lane's order
            placed = place(
                s, client, store, shot, note, sized.contracts,
                order_key=order_key, before_order=before_order,
            )
        note = placed.note
        if placed.filled > 0:
            # Record what actually filled, not what was asked for: a partial fill is a smaller bet.
            # Keep the fraction. int(1.28) stored a 1.28-contract fill as 1, so the win was booked at $1.
            sized = Sizing(round(placed.filled, 2), placed.cost, sized.why)
        else:
            bet = False  # blocked, refused, failed or unfilled: there is no position to track
    action = "bet" if bet else "pass"
    held = {"contracts": sized.contracts, "stake_usd": sized.cost_usd} if bet and sized.ok else {}
    _log(
        store, s, shot, model_p=p, edge=e, action=action, reason=note[:300],
        context=got.context[:1500], cal_p=cal.p, side=shot.side, **held,
    )
    return Outcome(shot.ticker, shot.title, shot.ask, p, e, action, note, shot.lane)


# Kalshi keeps a separate balance for each exchange_index a market trades on. A market whose index holds no cash refuses
# every order with insufficient_balance, however much cash the account has elsewhere. Remember it, so the bot says why once
# and then stops asking instead of failing every minute.
_EXCHANGE_OF: dict[str, int] = {}  # series -> exchange_index
_UNFUNDED: dict[int, float] = {}  # exchange_index -> do not try again before this time
UNFUNDED_PAUSE = 600.0


def _explain_unfunded(client: KalshiClient, shot: LongShot, cost: float) -> str | None:
    """If a refused order was a balance problem on the market's own exchange, say so and pause orders there."""
    try:
        idx = client.market(shot.ticker).get("exchange_index")
        rows = client.balance().get("balance_breakdown") or []
    except KalshiError:
        return None
    if idx is None:
        return None
    by_index = {int(r.get("exchange_index", -1)): float(r.get("balance") or 0.0) for r in rows}
    here = by_index.get(int(idx), 0.0)
    if here + 1e-9 >= cost:
        return None
    _EXCHANGE_OF[shot.series] = int(idx)
    _UNFUNDED[int(idx)] = time.time() + UNFUNDED_PAUSE
    elsewhere = ", ".join(f"exchange {i}: ${b:.2f}" for i, b in sorted(by_index.items()) if b > 0) or "none"
    return (f"skipped: {shot.series} trades on Kalshi exchange {idx}, which holds ${here:.2f} of your cash "
            f"(cash is on {elsewhere}). Move funds there in the Kalshi app; orders for it are paused for "
            f"{UNFUNDED_PAUSE / 60:.0f} minutes")


def dwell_seconds(s: Settings, shot: LongShot, floor: float, now: float | None = None) -> float | None:
    """How many seconds the side we would buy has been at or above `floor` (the lane's lowest price), from the tape's quotes of the last 15 minutes. None when the tape has nothing to say."""
    from . import db as database

    now = time.time() if now is None else now
    try:
        tape = database.connect(s.tape_path, "tape", shared=True)
        rows = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts<=? AND ts>? AND bid > 0 AND ask > 0 ORDER BY ts DESC", (shot.ticker, now, now - 900.0)).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    since = now  # the earliest quote of the unbroken run at or above the floor, newest first
    for ts, bid, ask in rows:
        price = ask if shot.side == "yes" else 1.0 - bid
        if price < floor:
            return now - since
        since = ts
    return now - since


def high_since(s: Settings, ticker: str, side: str, since: float, now: float | None = None) -> float | None:
    """The side's highest bid on the tape since `since`. None when the tape has nothing to say."""
    from . import db as database

    now = time.time() if now is None else now
    try:
        tape = database.connect(s.tape_path, "tape", shared=True)
        rows = tape.execute(
            "SELECT bid, ask FROM quotes WHERE ticker=? AND ts>=? AND ts<=? AND bid > 0 AND ask > 0",
            (ticker, since, now),
        ).fetchall()
    except Exception:
        return None
    prices = [float(bid) if side == "yes" else 1.0 - float(ask) for bid, ask in rows]
    return max(prices) if prices else None


def below_line_seconds(s: Settings, ticker: str, side: str, line: float, now: float | None = None) -> float | None:
    """How many seconds the side's bid has been unbroken at or below `line`, from the tape's quotes of the last 15 minutes. None when the tape has nothing to say (the socket watcher measures the same run live; this is only its backstop agreeing with it)."""
    from . import db as database

    now = time.time() if now is None else now
    try:
        tape = database.connect(s.tape_path, "tape", shared=True)
        rows = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts<=? AND ts>? AND bid > 0 AND ask > 0 ORDER BY ts DESC", (ticker, now, now - 900.0)).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    since = now  # the earliest quote of the unbroken run at or below the line, newest first
    for ts, bid, ask in rows:
        price = bid if side == "yes" else 1.0 - ask
        if price > line + 1e-9:
            return now - since
        since = ts
    return now - since


def scalp_sweep_hit(s: Settings, spec: LaneSpec, ticker: str, side: str, entry: float, bid: float, opened: float, now: float) -> str | None:
    """The scalp exit for the periodic sweep, which has the tape instead of a live socket: the bid is scalp_dip under the high since the bet, and has been for scalp_dip_seconds
    (from the tape; if the tape cannot say how long, no sale on a guess). The socket watcher applies the same rule to its own timer."""
    arms = [p for p in (spec.scalp_at, entry + spec.scalp_up if spec.scalp_up > 0 else 0.0) if p > 0]
    if not arms:
        return None
    high = high_since(s, ticker, side, opened, now)
    why = None if high is None else scalp_hit(bid, high, min(arms), spec.scalp_dip)
    if why is None or high is None or spec.scalp_dip_seconds <= 0:
        return why
    if spec.scalp_dip_hard > 0 and high - bid >= spec.scalp_dip_hard - 1e-9:
        return f"{why} (hard drop)"
    held = below_line_seconds(s, ticker, side, high - spec.scalp_dip, now)
    return f"{why} for {held:.0f}s" if held is not None and held >= spec.scalp_dip_seconds else None


def _hovering(s: Settings, ticker: str, side: str, bid: float, now: float) -> bool:
    """The last 3 seconds barely moved, and there is more than one print. The socket uses the same test."""
    from . import db as database

    try:
        tape = database.connect(s.tape_path, "tape", shared=True)
        rows = tape.execute(
            "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts<=? AND ts>? AND bid > 0 AND ask > 0 ORDER BY ts",
            (ticker, now, now - 3.0),
        ).fetchall()
    except Exception:
        return False
    points = [(float(ts), float(qbid) if side == "yes" else 1.0 - float(ask)) for ts, qbid, ask in rows]
    recent = [b for ts, b in points if now - 3.0 <= ts <= now and b > 0]
    return len(recent) >= 2 and price_shape(points, now, bid) == "hover"


def _not_a_downtrend(s: Settings, ticker: str, side: str, bid: float, now: float, *, hold_at: float | None = 0.50) -> bool:
    """True when the last 3 seconds are a spike or a hover, or the bid is still at `hold_at` or above, so the stop holds. hold_at None skips that price check."""
    if hold_at is not None and bid >= hold_at:
        return True
    from . import db as database

    try:
        tape = database.connect(s.tape_path, "tape", shared=True)
        rows = tape.execute(
            "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts<=? AND ts>? AND bid > 0 AND ask > 0 ORDER BY ts",
            (ticker, now, now - 30.0),
        ).fetchall()
    except Exception:
        return True
    points = [(float(ts), float(qbid) if side == "yes" else 1.0 - float(ask)) for ts, qbid, ask in rows]
    return price_shape(points, now, bid) != "trend"


def side_trend(s: Settings, shot: LongShot, seconds: float, now: float | None = None) -> float | None:
    """How far the price of the side we would buy (the mid of its bid and ask, in its own terms) has moved over the last `seconds`, from the tape's quotes. None when the tape has no quote that far back or none lately."""
    from . import db as database

    now = time.time() if now is None else now
    try:
        tape = database.connect(s.tape_path, "tape", shared=True)
        last = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts<=? AND ts>? AND bid > 0 AND ask > 0 ORDER BY ts DESC LIMIT 1", (shot.ticker, now, now - 30.0)).fetchone()
        then = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts<=? AND ts>? AND bid > 0 AND ask > 0 ORDER BY ts DESC LIMIT 1", (shot.ticker, now - seconds, now - seconds - 60.0)).fetchone()
    except Exception:
        return None
    if last is None or then is None:
        return None

    def price(row: Any) -> float:
        mid = (row[1] + row[2]) / 2.0
        return mid if shot.side == "yes" else 1.0 - mid

    return price(last) - price(then)


SPOT_STALE = 30.0  # a spot price older than this says nothing about now
SPOT_LOOKBACK = 40.0  # seconds of spot prices read to find the last changes


def _spot_symbols(series: str) -> list[str]:
    """Where the underlying's price is in the tape's feed_ticks: Coinbase's product first (it ticks most often), then the instrument's own symbol."""
    from .commodities import symbol_for

    inst = symbol_for(series)
    if not inst:
        return []
    out = []
    if inst.startswith("kraken:") and "/" in inst:
        out.append("coinbase:" + inst.split(":", 1)[1].replace("/", "-"))
    out.append(inst)
    return out


def _spot_prices(s: Settings, shot: LongShot, seconds: float, now: float) -> list[tuple[float, float]]:
    """(ts, price) of the underlying over the last `seconds`, oldest first, from the first feed that has a price newer than SPOT_STALE. [] when there is none."""
    from . import db as database

    try:
        tape = database.connect(s.tape_path, "tape", shared=True)
        for symbol in _spot_symbols(shot.series or shot.ticker.split("-")[0]):
            rows = tape.execute("SELECT ts, px FROM feed_ticks WHERE symbol=? AND ts<=? AND ts>? ORDER BY ts", (symbol, now, now - seconds)).fetchall()
            if rows and now - float(rows[-1][0]) <= SPOT_STALE:
                return [(float(a), float(b)) for a, b in rows]
    except Exception:
        return []
    return []


def spot_ticks_aligned(s: Settings, shot: LongShot, ticks: int, now: float | None = None) -> bool | None:
    """Whether the underlying's last `ticks` price changes (repeated prices skipped) all moved the way this bet goes: up for YES, down for NO. None when the tape has no fresh price or too few changes to say."""
    now = time.time() if now is None else now
    prices = _spot_prices(s, shot, SPOT_LOOKBACK, now)
    changes = [b[1] - a[1] for a, b in itertools.pairwise(prices) if b[1] != a[1]]
    if len(changes) < ticks:
        return None
    sign = 1.0 if shot.side == "yes" else -1.0
    return all(sign * c > 0 for c in changes[-ticks:])


def spot_move(s: Settings, shot: LongShot, seconds: float, now: float | None = None) -> float | None:
    """The underlying's net move over the last `seconds`, signed so that positive goes the way this bet goes (up for YES, down for NO). None when the tape has no fresh price or none that far back."""
    now = time.time() if now is None else now
    prices = _spot_prices(s, shot, seconds + 60.0, now)
    old = [p for t, p in prices if t <= now - seconds]
    if not prices or not old:
        return None
    return (prices[-1][1] - old[-1]) * (1.0 if shot.side == "yes" else -1.0)


def side_trend_z(s: Settings, shot: LongShot, seconds: float, now: float | None = None) -> float | None:
    """The side's price move over the last `seconds` in units of that market's own noise: the move divided by the standard deviation of its `seconds`-long changes over the 2 minutes before. None when the tape has too little to say or the market has not moved."""
    import bisect
    import statistics

    from . import db as database

    now = time.time() if now is None else now
    try:
        tape = database.connect(s.tape_path, "tape", shared=True)
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts<=? AND ts>? AND bid > 0 AND ask > 0 ORDER BY ts", (shot.ticker, now, now - 150.0 - seconds)).fetchall()
    except Exception:
        return None
    if len(q) < 6 or now - q[-1][0] > 30.0:
        return None
    ts = [r[0] for r in q]
    mid = [((r[1] + r[2]) / 2.0 if shot.side == "yes" else 1.0 - (r[1] + r[2]) / 2.0) for r in q]

    def at(t: float) -> float | None:
        i = bisect.bisect_right(ts, t) - 1
        return mid[i] if i >= 0 else None

    cur, then = at(now), at(now - seconds)
    if cur is None or then is None:
        return None
    n = int(120 - seconds)
    grid = [at(now - 5.0 - k) for k in range(120)]
    steps = [a - b for a, b in zip(grid[:n], grid[int(seconds):int(seconds) + n], strict=False) if a is not None and b is not None]
    if len(steps) < 20:
        return None
    sd = statistics.pstdev(steps)
    if sd <= 1e-6:  # a market whose price never changed lately: no move is no gain, a move is a big one
        return 0.0 if abs(cur - then) < 1e-9 else math.copysign(10.0, cur - then)
    return (cur - then) / sd


def _day_start(now: float) -> float:
    from .risk import start_of_day

    return start_of_day(now)


def _fill_shard(s: Settings, client: KalshiClient, store: Store, idx: int, need: float, why: str) -> str | None:
    """Bring exchange `idx` up to `need` dollars, from the richest other exchange.

    None means it already holds that much, or the transfer landed. A string means it could not.
    The source is never left under SHARD_TOPUP_KEEP_USD, and a day never moves more than SHARD_TOPUP_MAX_USD_PER_DAY.
    When the exchange is short of the target as well as this need, the move aims at the target.
    """
    cash = client.shard_balances()
    here = cash.get(idx, 0.0)
    if here + 1e-9 >= need:
        _UNFUNDED.pop(idx, None)
        return None
    sources = {i: c - s.shard_topup_keep for i, c in cash.items() if i != idx and c - s.shard_topup_keep > 0}
    if not sources:
        return (f"skipped: exchange {idx} holds ${here:.2f}, and no other exchange has cash above the "
                f"${s.shard_topup_keep:.0f} floor to move in")
    src = max(sources, key=lambda i: sources[i])
    room = s.shard_topup_max_per_day - store.shard_moved_since(_day_start(time.time()))
    amount = round(min(max(need - here, s.shard_topup_target - here), sources[src], room), 2)
    if amount + 1e-9 < need - here:
        return (f"skipped: exchange {idx} needs ${need - here:.2f} more but the top-up limit allows "
                f"${max(amount, 0):.2f} (daily cap ${s.shard_topup_max_per_day:.0f})")
    resp = client.shard_transfer(amount, "event_contract", src, "event_contract", idx)
    store.add_shard_move(src, idx, amount, why, str(resp.get("transfer_id", "")))
    log.info("moved $%.2f from exchange %d to exchange %d (%s)", amount, src, idx, why)
    for _ in range(8):  # the transfer is asynchronous: wait for it to land
        time.sleep(1.0)
        if client.shard_balances().get(idx, 0.0) + 1e-9 >= need:
            _UNFUNDED.pop(idx, None)
            return None
    return f"skipped: moved ${amount:.2f} to exchange {idx} but it has not landed yet; the next review retries"


def _top_up(s: Settings, client: KalshiClient, store: Store, shot: LongShot, cost: float) -> str | None:
    """Make sure the exchange this order trades on holds enough cash, moving some in from another exchange if it does not.

    Returns None when the order may go ahead, or the reason it should be skipped. Opt-in (SHARD_TOPUP_TARGET_USD > 0).
    """
    if s.shard_topup_target <= 0 or not hasattr(client, "shard_balances"):
        return None
    try:
        idx = _EXCHANGE_OF.get(shot.series)
        if idx is None:
            raw = client.market(shot.ticker).get("exchange_index")
            if raw is None:
                return None
            idx = _EXCHANGE_OF[shot.series] = int(raw)
        if client.shard_balances().get(idx, 0.0) + 1e-9 >= cost:
            _UNFUNDED.pop(idx, None)
            return None
        note = _fill_shard(s, client, store, idx, cost, f"top up for {shot.ticker}")
        return note
    except KalshiRateLimited:
        raise
    except KalshiError as exc:
        return f"skipped: could not top up exchange cash ({exc})"


def fund_open_exchanges(s: Settings, client: KalshiClient, store: Store) -> None:
    """Before any order: each exchange that already holds a position should already hold the top-up target.

    A transfer takes a few seconds to land. Doing it here keeps that wait off the moment a moonshot print qualifies.
    An exchange with no open position is left alone; the first order there still tops itself up.
    """
    if s.shard_topup_target <= 0 or not getattr(client, "authed", False):
        return
    from .manual import parse_positions

    try:
        held = parse_positions(client.positions())
    except KalshiError as exc:
        log.warning("could not read open positions before funding exchanges: %s", exc)
        return
    indexes: set[int] = set()
    for pos in held:
        try:
            raw = client.market(pos.ticker).get("exchange_index")
        except KalshiError as exc:
            log.warning("could not read the exchange for %s: %s", pos.ticker, exc)
            continue
        if raw is None:
            continue
        idx = int(raw)
        indexes.add(idx)
        _EXCHANGE_OF[pos.ticker.split("-")[0]] = idx
    if not indexes:
        log.info("no open position, so no exchange was funded ahead of trading")
        return
    for idx in sorted(indexes):
        try:
            note = _fill_shard(s, client, store, idx, s.shard_topup_target, "before trading")
        except KalshiRateLimited:
            raise
        except KalshiError as exc:
            log.warning("could not fund exchange %s before trading: %s", idx, exc)
            continue
        if note:
            log.warning("exchange %s was not funded before trading: %s", idx, note)
        else:
            log.info("exchange %s holds at least $%.2f before trading", idx, s.shard_topup_target)


class Placed(NamedTuple):
    note: str
    filled: float = 0.0  # contracts that actually filled
    cost: float = 0.0  # dollars spent on them, fee included
    price: float = 0.0  # a resting buy's own price, which can differ from the ask it was planned at (0 = not a resting buy)


_LANE_LOCKS: dict[str, threading.Lock] = {}
_LANE_LOCKS_GUARD = threading.Lock()
_ORDER_LOCK = threading.Lock()  # the risk check, the order and its ledger row happen as one step for an immediate buy: two workers cannot both pass a global cap and then both buy


def lane_lock(lane: str) -> threading.Lock:
    """The lock one lane's evaluations take, so buy workers can run different lanes at the same time without two of one lane racing past its own limits."""
    with _LANE_LOCKS_GUARD:
        return _LANE_LOCKS.setdefault(lane, threading.Lock())


_NO_FRACTION: set[str] = set()  # filled once Kalshi has refused a fractional order that the whole-contract retry then got through: stop asking for fractions


def place(
    s: Settings, client: KalshiClient, store: Store, shot: LongShot, why: str, count: float,
    order_key: str | None = None, before_order: Callable[[], None] | None = None,
) -> Placed:
    """Send the order for `count` contracts (sized by sizing.py) if the book and risk gate agree."""
    cost = shot.ask * count + order_fee(shot.ask, count)
    if cost + 1e-9 < s.min_order_usd:
        return Placed(f"skipped: order ${cost:.2f} is under the ${s.min_order_usd:.2f} minimum")
    # The order is immediate-or-cancel at a limit. At the ask it fails whenever the ask has ticked up since it was read (13% of real buys did); a lane may allow a few cents
    # more. The order still fills at the best ask and pays the allowance only if the book has moved, so the most it can cost is the limit times the count: that is what is checked.
    lane_spec = lane_specs(s).get(shot.lane)
    limit = round(max(shot.ask, min(0.99, shot.ask + max(0.0, lane_spec.buy_slip if lane_spec is not None else 0.0))), 4)  # four decimals, the exchange's scale: rounding to cents could land under a sub-cent ask; never under the ask, never allowance past 99c
    cost = limit * count + order_fee(limit, count)
    cap = cost  # already sized under every ceiling; the gate re-checks the global ones
    try:
        with steptime.step("order book"):
            book = client.orderbook(shot.ticker)
        offered = side_ask_notional(book, limit, shot.side)
    except KalshiError as exc:
        return Placed(f"skipped: could not read the book ({exc})")
    if lane_spec is not None and lane_spec.buy_max_move > 0:
        fresh = best_ask(book, shot.side)
        if fresh is not None and fresh < shot.ask - lane_spec.buy_max_move - 1e-9:
            return Placed(f"skipped: the {shot.side} ask fell from {shot.ask:.2f} to {fresh:.2f} while the order waited (the lane allows a fall of {lane_spec.buy_max_move:.2f})")
    if offered + 1e-9 < s.min_order_usd:
        return Placed(
            f"skipped: ${offered:.2f} offered at {limit:.2f}, "
            f"under the ${s.min_order_usd:.2f} minimum"
        )
    with steptime.step("risk check"):
        verdict = risk.check(
            s, store, shot.ticker, cost, max_trade=cap,
            event_ticker=shot.event_ticker, mutually_exclusive=shot.mutually_exclusive,
        )
    if not verdict.ok:
        return Placed(f"blocked by risk: {verdict.reason}")
    with steptime.step("top up"):
        topped = _top_up(s, client, store, shot, cost)
    if topped:
        return Placed(topped)
    known = _EXCHANGE_OF.get(shot.series) if hasattr(client, "shard_balances") else None
    if known is not None and _UNFUNDED.get(known, 0.0) > time.time():
        return Placed(f"skipped: exchange {known} (where {shot.series} trades) has no cash; move funds there in the Kalshi app")
    if before_order:
        before_order()
    if lane_spec is not None and lane_spec.buy_rest_seconds > 0:
        placed = _place_resting(s, client, store, shot, why, count, lane_spec, order_key, book)
        if placed.filled > 0:
            entry = placed.price or round(max(0.01, shot.ask - lane_spec.buy_rest_below), 4)  # a post-only buy fills at its own limit, so this is the price paid
            note = _rest_take_profit(s, client, store, shot, lane_spec, placed.filled, entry)
            if note:
                return Placed(f"{placed.note} {note}", placed.filled, placed.cost, placed.price)
        return placed
    buy = client.buy_yes_ioc if shot.side == "yes" else client.buy_no_ioc
    retries = lane_spec.buy_retries if lane_spec is not None else 0
    first_at = time.time()
    key = order_key
    attempt = 0
    while True:
        try:
            try:
                with steptime.step("order"):
                    resp = buy(shot.ticker, count, limit, client_order_id=key)
            except KalshiRateLimited:
                raise  # refused outright, so nothing was bought; the queue retries it later
            except KalshiError as exc:
                if count == int(count) or int(count) < 1 or "insufficient_balance" in str(exc):
                    raise
                # A fractional order was refused: ask for the whole contracts that fit, and stop asking for fractions if that goes through.
                log.warning("fractional order for %s refused (%s); retrying %d whole contracts", shot.ticker, exc, int(count))
                count = int(count)
                resp = buy(shot.ticker, count, limit, client_order_id=key)
                _NO_FRACTION.add("refused")
        except KalshiRateLimited:
            raise
        except KalshiError as exc:
            if "insufficient_balance" in str(exc):
                why_not = _explain_unfunded(client, shot, cost)
                if why_not:
                    return Placed(why_not)
            return Placed(f"order failed: {exc}")
        filled = float(resp.get("fill_count") or 0.0)
        avg = float(resp.get("average_fill_price") or shot.ask)
        fee = float(resp.get("average_fee_paid") or 0.0) * filled
        store.add_order(
            ticker=shot.ticker, env=s.env, count=count, price=limit, fill_count=filled,
            cost_usd=avg * filled + fee, fee_usd=fee, order_id=resp.get("order_id", ""),
            contract=shot.side,
        )
        spent = avg * filled + fee
        if filled > 0 or lane_spec is None or attempt >= retries or time.time() - first_at > lane_spec.buy_retry_seconds:
            break
        again = _requote(s, client, store, shot, lane_spec, count)
        if again is None:
            break
        attempt += 1
        limit = again
        key = f"{order_key}-r{attempt}" if order_key else None  # a new id: Kalshi refuses one it has seen, even on an order that filled nothing
        log.info("buy %s %s: nothing filled, trying again at %.2f (try %d of %d)", shot.ticker, shot.side, limit, attempt + 1, retries + 1)
    rest_note = _rest_take_profit(s, client, store, shot, lane_spec, filled, avg) if filled > 0 else ""
    return Placed(
        f"bought {shot.side.upper()} {filled:g}/{count} @ {avg:.4f} ({s.env}). {why}" + (f" {rest_note}" if rest_note else ""), filled, spent
    )


def _requote(s: Settings, client: KalshiClient, store: Store, shot: LongShot, spec: LaneSpec, count: float) -> float | None:
    """The limit for a second try after an immediate buy filled nothing, or None to give up. It reads the book again and prices the order off the fresh ask, but never above the first limit plus
    `buy_retry_chase`, never outside the lane's price band, and only if the risk gate still agrees at that price: a market that ran away is not chased."""
    try:
        ask = best_ask(client.orderbook(shot.ticker), shot.side)
    except KalshiError:
        return None
    if ask is None or ask < spec.price[0] - 1e-9 or ask > spec.price[1] + 1e-9:
        return None
    limit = round(min(0.99, ask + max(0.0, spec.buy_slip)), 4)
    if limit > shot.ask + max(0.0, spec.buy_slip) + max(0.0, spec.buy_retry_chase) + 1e-9:
        return None
    cost = limit * count + order_fee(limit, count)
    verdict = risk.check(s, store, shot.ticker, cost, max_trade=cost, event_ticker=shot.event_ticker, mutually_exclusive=shot.mutually_exclusive)
    return limit if verdict.ok else None


def _resting_price(shot: LongShot, spec: LaneSpec, book: dict[str, Any] | None) -> tuple[float | None, str]:
    """(the price a resting buy sits at, "") or (None, why it is skipped). It rests `buy_rest_below` under the ask. The ask was read some time ago and may have fallen since; a post-only buy at or over the ask
    is refused ("post only cross": 94% of 275 refusals in two days), so when the live book's ask is lower it rests the same distance under that one. A fallen ask under the lane's price floor skips the buy.
    A risen ask does not raise the price, and a missing or empty book leaves the planned price."""
    price = round(max(0.01, shot.ask - spec.buy_rest_below), 4)
    if price >= shot.ask - 1e-9:
        return None, f"skipped: a resting buy needs a price under the ask {shot.ask:.2f}"
    live_ask = best_ask(book, shot.side) if book else None
    if live_ask is not None and live_ask < shot.ask - 1e-9:
        if live_ask < spec.price[0] - 1e-9:
            return None, f"skipped: the {shot.side} ask fell from {shot.ask:.2f} to {live_ask:.2f}, under the lane's {spec.price[0]:.2f} floor"
        lowered = round(max(0.01, live_ask - spec.buy_rest_below), 4)
        if lowered < price:
            log.info("resting buy %s %s: the ask fell from %.2f to %.2f, resting at %.2f instead of %.2f", shot.ticker, shot.side, shot.ask, live_ask, lowered, price)
            price = lowered
    return price, ""


def _place_resting(s: Settings, client: KalshiClient, store: Store, shot: LongShot, why: str, count: float, spec: LaneSpec, order_key: str | None, book: dict[str, Any] | None = None) -> Placed:
    """Rest a post-only buy `spec.buy_rest_below` under the ask for `spec.buy_rest_seconds`, then report what filled. Nothing is bought if it does not fill. A resting order that fills is charged no taker fee, and the fee Kalshi charged is recorded as it came."""
    from . import account

    price, skip = _resting_price(shot, spec, book)
    if price is None:
        return Placed(skip)
    started = time.time()
    try:
        resp = client.buy_resting(shot.ticker, shot.side, count, price, spec.buy_rest_seconds, client_order_id=order_key)
    except KalshiRateLimited:
        raise
    except KalshiError as exc:
        if "post only cross" not in str(exc):
            return Placed(f"resting buy refused: {exc}")
        # Refused outright, so nothing was placed and another try cannot double up. The book moves faster than the order travels (a thin market's ask flickers about once a second), so look again and rest under the ask as it is now, once.
        try:
            fresh = client.orderbook(shot.ticker)
        except KalshiError:
            return Placed(f"resting buy refused: {exc}")
        price, skip = _resting_price(shot, spec, fresh)
        if price is None:
            return Placed(skip)
        started = time.time()
        try:
            # Kalshi remembers the client_order_id of the order it refused, so the same one again is a duplicate (409 order_already_exists). The retry's own id comes from the same key: a re-run of this job repeats it rather than doubling up.
            resp = client.buy_resting(shot.ticker, shot.side, count, price, spec.buy_rest_seconds, client_order_id=f"{order_key}-retry" if order_key else None)
        except KalshiRateLimited:
            raise
        except KalshiError as exc2:
            return Placed(f"resting buy refused: {exc2}")
    order_id = resp.get("order_id") or (resp.get("order") or {}).get("order_id") or ""
    filled = spent = fee_paid = 0.0
    seen: set[str] = set()
    deadline = started + spec.buy_rest_seconds + 3.0
    while time.time() < deadline and filled < count - 1e-9:
        time.sleep(2.0)
        try:
            rows = client.portfolio_rows("/portfolio/fills", "fills", int(started) - 5, pages=1)
        except KalshiError:
            continue
        for f in rows:
            row = account.fill_row(f)
            if row is None or row[9] != order_id or row[0] in seen:
                continue
            seen.add(row[0])
            n, yes_price, fee = float(row[4]), float(row[5]), float(row[7] or 0.0)
            own = yes_price if shot.side == "yes" else 1.0 - yes_price
            filled += n
            spent += own * n + fee
            fee_paid += fee
    store.add_order(
        ticker=shot.ticker, env=s.env, count=count, price=price, fill_count=filled, cost_usd=spent, fee_usd=fee_paid, order_id=order_id, contract=shot.side,
    )
    if filled <= 0:
        return Placed(f"resting buy at {price:.2f} for {spec.buy_rest_seconds:.0f}s did not fill (the ask was {shot.ask:.2f}); nothing bought")
    return Placed(f"bought {shot.side.upper()} {filled:g}/{count} resting at {price:.2f}, fee ${fee_paid:.4f}. {why}", filled, spent, price)


def rest_target_price(spec: LaneSpec, entry: float) -> float | None:
    """The price a resting take-profit sell sits at: the lane's take-profit multiple of the price paid, or its fixed price.

    None when the lane has no target, or none the bid could ever reach: the same test take_profit_hit makes
    (a multiple that would need $1.00 or more is not a target, it is holding to settlement)."""
    if entry <= 0:
        return None
    if spec.take_profit_x > 0 and entry * spec.take_profit_x < 1.0:
        return round(min(0.99, entry * spec.take_profit_x), 4)
    if spec.take_profit_at > 0 and entry < spec.take_profit_at:
        return round(min(0.99, spec.take_profit_at), 4)
    return None


def _rest_take_profit(s: Settings, client: KalshiClient, store: Store, shot: LongShot, spec: LaneSpec | None, filled: float, entry: float) -> str:
    """Rest this lane's take-profit sell on the book, post-only, until the market closes. Returns a note for the bet's log line ('' = nothing rested).

    The order is placed before the price reaches the target, which is the whole point: when the bid gets there it fills
    as a maker and is charged no taker fee, instead of the watcher noticing and crossing the book. It is reduce-only, so
    it can never sell more than the buy just filled. Any other exit (a stop, a crash) cancels it first and sells what is
    left itself (exit_position). A refusal (the bid is already at the target, so post-only will not cross to it, or the
    API balks at the order) is only a note: the watcher still sells at the target the immediate way. Never raises: the
    buy has already filled, and losing the resting sell must not lose the bet's bookkeeping with it.
    """
    if spec is None or not spec.rest_sell or filled <= 0:
        return ""
    price = rest_target_price(spec, entry)
    if price is None:
        return ""
    try:
        close = datetime.fromisoformat(shot.close_time)
    except (TypeError, ValueError):
        return ""  # no known close, no honest expiry: the watcher sells at the target instead
    if close.tzinfo is None:
        close = close.replace(tzinfo=UTC)
    seconds = close.timestamp() - time.time() - 5.0  # die with the market; Kalshi cancels orders at the close anyway
    if seconds < 30:
        return ""  # no time for a resting order to mean anything; the watcher sells at the target itself
    try:
        resp = client.sell_resting(shot.ticker, shot.side, round(filled, 2), price, seconds)
    except KalshiError as exc:
        return f"resting take-profit at {price:.2f} refused ({exc}); the watcher will sell at the target instead."
    order_id = resp.get("order_id") or (resp.get("order") or {}).get("order_id") or ""
    if not order_id:
        return f"resting take-profit at {price:.2f} sent but Kalshi named no order; the watcher will sell at the target instead."
    store.add_order(
        ticker=shot.ticker, env=s.env, count=round(filled, 2), price=price, fill_count=0.0,
        cost_usd=0.0, fee_usd=0.0, order_id=order_id, side="rest", contract=shot.side,
    )
    return f"take-profit rests at {price:.2f} until the close (order {order_id}): if the bid reaches it, it sells with no taker fee."


def _book_resting_fills(s: Settings, client: KalshiClient, store: Store, ticker: str, cancel: bool = False) -> tuple[float, float, float]:
    """Book what the resting take-profit sell on this ticker has filled, as ordinary sell orders. Returns (newly booked, booked in all, their average fill price).

    Fills are read from Kalshi's fills list, matched by order id; each carries the fee Kalshi actually charged
    (none, for an order that rested first), which is what gets recorded. With `cancel`, the order is cancelled
    first and then retired: whatever filled before the cancel landed is booked here, and the caller sells what
    is left itself. Never raises: a fills read that fails just books nothing this time.
    """
    from . import account

    rest = store.rest_sell(ticker, s.env)
    if rest is None:
        return 0.0, 0.0, 0.0
    order_id = rest["order_id"]
    if cancel and store.working_rest_sell(ticker, s.env) is not None:
        with contextlib.suppress(KalshiError):
            client.cancel_order(order_id)
    booked, _ = store.booked_rest_sell(order_id)
    new = 0.0
    fills: list[tuple] = []
    with contextlib.suppress(KalshiError):
        for f in client.portfolio_rows("/portfolio/fills", "fills", int(float(rest["ts"])) - 5, pages=1):
            row = account.fill_row(f)
            if row is not None and row[9] == order_id:
                fills.append(row)
    fills.sort(key=lambda r: r[1])
    skip = booked
    for r in fills:
        n = float(r[4])
        if skip >= n - 1e-9:
            skip -= n
            continue
        take = n - max(skip, 0.0)
        skip = 0.0
        own = float(r[5]) if (rest["contract"] or "yes") == "yes" else 1.0 - float(r[5])
        fee = float(r[7] or 0.0) * (take / n if n else 0.0)
        store.add_order(
            ticker=ticker, env=s.env, count=take, price=round(own, 4), fill_count=take,
            cost_usd=-(own * take - fee), fee_usd=fee, order_id=order_id, side="sell", contract=rest["contract"] or "yes",
        )
        new += take
    if cancel:
        store.retire_rest_sell(order_id)
    total, avg = store.booked_rest_sell(order_id)
    return new, total, avg


def _hours_left(close_time: str, now: datetime | None = None) -> float | None:
    now = now or datetime.now(UTC)
    try:
        close = datetime.fromisoformat(close_time)
    except (TypeError, ValueError):
        return None
    if close.tzinfo is None:
        close = close.replace(tzinfo=UTC)
    return (close - now).total_seconds() / 3600.0


_EXIT_LOCK = threading.Lock()  # one exit at a time, so the periodic pass and the live watcher never sell the same bet twice


EXIT_RETRY_SLIP = (0.02, 0.05, 0.10)  # if the first sale fills nothing (or part), sell the rest this far under a fresh read of the best bid, three more times
EXIT_SLIP = 0.02  # an exit order is limited this far under the bid we saw: it is immediate-or-cancel, so it still fills at the best bid


def _ask_flip(s: Settings, store: Store, row, lane: str, why: str) -> None:
    """A stop just sold this bet: queue a look at the other side (reverse.py). Never lets a failure here touch the sale."""
    try:
        from . import reverse

        reverse.request(s, store, row["ticker"], row["side"] or "yes", lane, float(row["stake_usd"] or 0.0), why)
    except Exception:
        log.debug("flip request failed", exc_info=True)


def exit_position(
    s: Settings, client: KalshiClient, store: Store, row, bid: float, why: str,
    before_order: Callable[[], None] | None = None, slip: float = EXIT_SLIP,
) -> str:
    """Sell (or paper-sell) one open bet near `bid`. Safe to call from any thread.

    The price we saw may be a moment old by the time the order lands, and an immediate-or-cancel order at a stale bid fills nothing.
    So the order is limited `slip` below it: it still fills at the best bid on the book, and only a move bigger than the slip stops it.
    """
    with _EXIT_LOCK:
        lane = row["lane"] or "moonshot"
        if (row["ticker"], lane) not in {(r["ticker"], r["lane"] or "moonshot") for r in store.open_bets()}:
            return f"{row['ticker']}: already exited"
        side = row["side"] or "yes"
        seen = factors.snapshot_now(s, row["ticker"], side, row["close_time"], row["factors"] or "")  # the context at the moment of the sale, saved with the exit
        if not s.is_live(lane, row["ticker"].split("-")[0]):
            price = bid
            if why.startswith("take-profit"):
                # A lane that rests its sale (rest_sell) would have filled at the target the moment the bid touched
                # it, so the paper sale books the target, not the bid this re-read happens to catch. Booking the
                # re-read bid would grade the resting sell by a slippage it does not have.
                spec = lane_specs(s).get(lane)
                target = rest_target_price(spec, float(row["ask"] or 0.0)) if spec is not None and spec.rest_sell else None
                if target is not None:
                    price = target
            store.mark_exited(row["ticker"], price, why, lane, seen)
            _ask_flip(s, store, row, lane, why)
            return f"paper-sold {row['ticker']} @ {price:.2f}. {why}"
        # A resting take-profit sell may be working on this position: cancel it and book whatever it filled
        # before selling anything ourselves, or the two orders would race for the same contracts.
        _new, rest_filled, rest_avg = _book_resting_fills(s, client, store, row["ticker"], cancel=True)
        count = round(store.net_contracts(row["ticker"], s.env), 2)
        if count < 0.005 and rest_filled > 0:
            store.mark_exited(row["ticker"], rest_avg, f"resting take-profit filled at {rest_avg:.4f} (no taker fee)", lane, seen)
            _ask_flip(s, store, row, lane, why)
            return f"sold {rest_filled:g} {row['ticker']} resting @ {rest_avg:.4f} (no taker fee); the {why} sale found nothing left"
        if count <= 0:
            store.mark_exited(row["ticker"], bid, why, lane, seen)
            return f"paper-sold {row['ticker']} @ {bid:.2f} (no live fill). {why}"
        if before_order:
            before_order()
        try:
            sell = client.sell_yes_ioc if side == "yes" else client.sell_no_ioc
            resp = sell(row["ticker"], count, max(0.01, round(bid - slip, 2)))
        except KalshiRateLimited:
            raise
        except KalshiError as exc:
            return f"exit {row['ticker']}: sell failed ({exc})"
        filled = float(resp.get("fill_count") or 0.0)
        avg = float(resp.get("average_fill_price") or bid)
        fee = float(resp.get("average_fee_paid") or 0.0) * filled
        store.add_order(
            ticker=row["ticker"], env=s.env, count=count, price=bid, fill_count=filled,
            cost_usd=-(avg * filled - fee), fee_usd=fee, order_id=resp.get("order_id", ""),
            side="sell", contract=side,
        )
        proceeds, sold = avg * filled, filled
        left = store.net_contracts(row["ticker"], s.env)
        if left >= 0.005 and not why.startswith("take-profit"):
            # A sale into a book that has just been cleared fills nothing at a bid read a moment ago (burst and crash exits do this). Read the book again and sell what is left a little lower each time.
            for extra in EXIT_RETRY_SLIP:
                time.sleep(0.4)
                try:
                    fresh = best_bid(client.orderbook(row["ticker"]), side)
                except KalshiError:
                    fresh = None
                if fresh is None:
                    break  # nobody is bidding: nothing to sell into
                try:
                    r2 = sell(row["ticker"], round(left, 2), max(0.01, round(fresh - extra, 2)))
                except KalshiRateLimited:
                    raise
                except KalshiError:
                    break
                f2 = float(r2.get("fill_count") or 0.0)
                a2 = float(r2.get("average_fill_price") or fresh)
                fee2 = float(r2.get("average_fee_paid") or 0.0) * f2
                store.add_order(
                    ticker=row["ticker"], env=s.env, count=left, price=fresh, fill_count=f2,
                    cost_usd=-(a2 * f2 - fee2), fee_usd=fee2, order_id=r2.get("order_id", ""), side="sell", contract=side,
                )
                proceeds += a2 * f2
                sold += f2
                left = store.net_contracts(row["ticker"], s.env)
                if left < 0.005:
                    break
            avg = proceeds / sold if sold > 0 else avg
            filled = sold
        if store.net_contracts(row["ticker"], s.env) < 0.005:
            store.mark_exited(row["ticker"], avg if filled else bid, why, lane, seen)
            _ask_flip(s, store, row, lane, why)
            return f"sold {filled:g}/{count} {row['ticker']} @ {avg:.4f}. {why}"
        return f"partial exit {filled:g}/{count} {row['ticker']} @ {avg:.4f}. {why}"


def stop_loss_pass(
    s: Settings,
    client: KalshiClient,
    store: Store,
    *,
    now: datetime | None = None,
    before_order: Callable[[], None] | None = None,
    client_for: Callable[[str], KalshiClient] | None = None,
) -> list[str]:
    """Check every open bet. Sell (or paper-sell) those that hit the stop or a take-profit level. Returns log lines.

    Take-profit is also watched live on the socket (watcher.py); this pass is the slower backstop for it.
    """
    specs = lane_specs(s)
    now = now or datetime.now(UTC)
    notes: list[str] = []
    for row in store.open_bets():
        spec = specs.get(row["lane"] or "moonshot")
        profit_on = spec is not None and takes_profit(spec)
        stop_on = (
            s.stop_loss_enabled
            and (spec is None or spec.stop_loss)
            and not (row["ticker"].split("-")[0].endswith("15M") and not s.stop_loss_fast)
        )
        if not (stop_on or profit_on):
            continue  # a 15-minute window is held to settlement (STOP_LOSS_15M=false) unless it takes profit
        hours = _hours_left(row["close_time"], now)
        if hours is None:
            continue
        book = client_for(row["lane"] or "moonshot") if client_for is not None else client
        try:
            # A paper bet may be read from the shared market cache; a real one always asks the venue, because a sale is priced from what it reads.
            with shared_for(book, 0.0 if row["mode"] == "trade" else s.discovery_eval_age, s.discovery_enabled):
                m = book.market(row["ticker"])
        except KalshiError as exc:
            notes.append(f"stop {row['ticker']}: market lookup failed ({exc})")
            continue
        if m.get("result") in {"yes", "no"}:
            continue
        side = row["side"] or "yes"
        quoted = flip(m) if side == "no" else m
        bid = 0.0
        with contextlib.suppress(TypeError, ValueError):
            bid = float(quoted.get("yes_bid_dollars") or 0.0)
        entry = float(row["ask"] or 0.0)
        why = None
        fifteen = row["ticker"].split("-")[0].endswith("15M")
        if profit_on and spec is not None:
            why = take_profit_hit(entry, bid, spec.take_profit_x, spec.take_profit_at)
            if not why:
                why = scalp_sweep_hit(s, spec, row["ticker"], side, entry, bid, float(row["ts"] or 0.0), now.timestamp())
        if not why and spec is not None and spec.cash_up_minute > 0 and fifteen:
            why = cash_if_up(entry, bid, 15.0 - hours * 60.0, spec.cash_up_minute, spec.cash_drop)
        left = hours * 3600.0
        if not why and spec is not None and spec.cash_left_seconds > 0 and 0 <= left <= spec.cash_left_seconds:
            why = cash_if_losing(entry, bid, left, spec.cash_left_seconds, _hovering(s, row["ticker"], side, bid, now.timestamp()))
        if not why and spec is not None and spec.cash_before_minute > 0 and fifteen and row["close_time"]:
            with contextlib.suppress(TypeError, ValueError):
                close = datetime.fromisoformat(row["close_time"]).timestamp()
                opened = 15.0 - (close - float(row["ts"])) / 60.0
                why = early_cash(
                    entry, bid, opened, 15.0 - hours * 60.0,
                    before=spec.cash_before_minute, leave=spec.cash_leave_minute,
                    hovering=_hovering(s, row["ticker"], side, bid, now.timestamp()),
                )
        owned = False
        if not why and stop_on and spec is not None and left > 0:
            if spec.watch_loss > 0 and entry > 0 and bid <= entry * (1.0 - spec.watch_loss):
                owned = True
                dipped = below_line_seconds(s, row["ticker"], side, entry * (1.0 - spec.watch_loss), now.timestamp())
                why = hard_loss(entry, bid, 0.0 if dipped is None else dipped, spec.watch_loss, spec.watch_loss_seconds)
            elif spec.watch_at > 0 and entry > spec.watch_at and bid <= spec.watch_at:
                owned = True
                trend = not _not_a_downtrend(s, row["ticker"], side, bid, now.timestamp(), hold_at=None)
                dipped = below_line_seconds(s, row["ticker"], side, spec.watch_at, now.timestamp())
                why = watch_trend(
                    entry, bid, left, 0.0 if dipped is None else dipped, line=spec.watch_at,
                    ride=spec.watch_ride_seconds, patient_left=spec.watch_patient_left, trend=trend,
                )
        if not why and stop_on and not owned:
            why = stop_loss_hit(entry, bid, hours, s.stop_loss_hours, stop_drop_for(spec, s))
        if not why:
            continue
        if why.startswith("take-profit") and store.working_rest_sell(row["ticker"], s.env) is not None:
            continue  # a resting sell is working this target: it fills with no taker fee, or not at all; crossing the book here would only pay the fee it exists to avoid
        if why.startswith("stop-loss") and "(watched)" not in why:
            # The same calculated wait the socket watcher applies (strategy.stop_wait), measured on the
            # tape: a backstop that sold mid-dip would rob the watcher of rebounds it is waiting for.
            # Last 2 minutes: sell only a downtrend that is already under 50c (strategy.price_shape).
            # A visit under 50c already waited, above.
            if fifteen and hours * 3600.0 <= 120 and _not_a_downtrend(s, row["ticker"], side, bid, now.timestamp()):
                continue
            wait = stop_wait(spec.stop_confirm_seconds if spec is not None else 0.0, hours * 3600.0, fifteen)
            if wait > 0:
                dipped = below_line_seconds(s, row["ticker"], side, entry * (1.0 - stop_drop_for(spec, s)), now.timestamp())
                if dipped is not None and dipped < wait:
                    continue
        notes.append(exit_position(s, book, store, row, bid, why, before_order, slip=EXIT_SLIP + (0.03 if why.startswith("stop-loss") else 0.0)))
    return notes


def next_stop_loss_at(s: Settings, store: Store, now: float | None = None) -> float | None:
    """When to check again. None if nothing is open."""
    now = time.time() if now is None else now
    soonest: float | None = None
    fast = False
    specs = lane_specs(s)
    for row in store.open_bets():
        spec = specs.get(row["lane"] or "moonshot")
        if spec is not None and takes_profit(spec) and _hours_left(row["close_time"]) is not None:
            soonest, fast = now, True  # a take-profit bet is checked every minute (the live watcher is faster)
            continue
        if (
            not s.stop_loss_enabled
            or (spec is not None and not spec.stop_loss)
            or (row["ticker"].split("-")[0].endswith("15M") and not s.stop_loss_fast)
        ):
            continue
        hours = _hours_left(row["close_time"])
        if hours is None:
            continue
        close_ts = now + hours * 3600
        window = close_ts - s.stop_loss_hours * 3600
        soonest = window if soonest is None else min(soonest, window)
    if soonest is None:
        return None
    if soonest <= now:
        return now + (60.0 if fast else s.stop_loss_check_minutes * 60)
    return soonest


def resolve(client: KalshiClient, store: Store, client_for: Callable[[str], KalshiClient] | None = None) -> int:
    """Fill in outcomes for markets that have settled. Returns how many markets resolved."""
    n = 0
    for ticker in store.unresolved_tickers():
        book = client_for(ticker) if client_for is not None else client
        try:
            m = book.market(ticker)
        except KalshiError:
            continue
        if m.get("result") in {"yes", "no"}:
            store.set_outcome(ticker, m["result"])
            n += 1
    return n
