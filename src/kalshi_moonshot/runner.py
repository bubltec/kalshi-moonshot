"""One scan pass over two lanes, then log every decision.

moonshot: cheap long shots, a small chance of a large payout.
steady:   likely outcomes whose ask has been rising, a small payout that repeats.

For each candidate the model gets the question, similar settled cases, and recent news
headlines. Orders are placed only when MODE=trade and the risk gate agrees.
"""

from __future__ import annotations

import bisect
import contextlib
import json
import logging
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from . import curverisk, factors, risk, shadow, steptime
from .calibration import Calibration
from .commodities import CommodityPricer, load_instruments
from .config import Settings
from .estimators import Estimate, EstimateShare, Estimator, build_estimators
from .kalshi import MARKET_PAGES, KalshiClient, KalshiError, KalshiRateLimited
from .lanes import (
    LADDER,
    LaneSpec,
    edge_verdict,
    lane_specs,
    main_lanes,
    settings_of,
    spec_for,
    tiered_lanes,
    version_of,
    wants_bet,
)
from .search import NewsSearch
from .sizing import Sizing, size_bet
from .store import Store
from .strategy import (
    LongShot,
    ask_trend,
    best_ask,
    best_bid,
    both_sides,
    both_sides_tag,
    edge,
    falling_reason,
    lane_candidates,
    lane_reject_reason,
    one_per_mutex_event,
    order_fee,
    rising,
    side_ask_notional,
    side_prob,
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
    if spec.buy_chain:  # the lane's own steps put the sides forward, in its order; the estimator's screen (the ladder's) is not asked which side to look at
        from . import buychain

        cands = buychain.screen(buychain.Ctx(s, spec, None, now.timestamp()), cands)
    elif estimator is not None:
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
    if s.tape_role == "record":  # the recorder process reads only what it records by (lanes.recorder_lanes): a lane setting its image does not know cannot stop the tape
        from .lanes import recorder_lanes

        for name, series in recorder_lanes(s):
            if s.live_lanes is None or name in s.live_lanes:
                out |= set(series) if series else {i.prefix + suffix for i in load_instruments(s.data_dir) for suffix in i.suffixes}
        return out
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
        quant.learn_windows(store, s, now)  # keep each 15-minute series' feed offset fresh, from the windows the stream and the history job have stored (no REST read)
    relaxed = replace(
        spec, price=(0.0, 1.0), max_spread=1.0, min_volume=0.0, min_return=0.0,
        entry_elapsed=(0.0, 1e9),  # a window not yet in its entry range must still be watched
        blackout_et=(),  # and a blackout ends: the watchlist must be ready the moment it does
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
                    {"close": m.get("close_time", ""), "category": category, "series": series, "ticker": m.get("ticker", ""), "strike": m.get("floor_strike")},
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
    if spec.buy_chain:
        from . import buychain

        dropped = buychain.DROPPED.pop(wide.name, None)
    else:
        dropped = getattr((estimators or {}).get(spec.estimator), "dropped", None)
    if dropped:
        store.log_screened(spec.name, dropped, now.timestamp())
    if spec.cooldown_minutes:
        # A short market must be re-read as it nears the close: its outcome is only decided late.
        # So the cooldown is at most a fifth of the time left.
        def cooling(x: LongShot) -> bool:
            minutes = min(spec.cooldown_minutes, max(0.5, x.hours_to_close * 60.0 / 5.0))
            return store.decided_since(x.ticker, now.timestamp() - minutes * 60, spec.name)  # this lane's own decisions only: another lane's reading of the market is no reason for this lane to skip it

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


REASON_MAX = 4000  # a bound, not a cut: a decision's whole note is stored (a long one is about 900 characters). Until 2026-11-17 the first 300 were kept, a limit from when a note was one sentence; the column is text and was never the constraint (user: "why is it limited to 300chars? why don't we fix that??!")
_TAG = re.compile(r"\[[^\[\]]*\]")


def stored_reason(note: str) -> str:
    """The decision's note as it is stored: what the rules SAID first, the bracketed readings after it.
    2026-11-17, user: "my only issue is the number of buys. are we flagging why we turn things down?" We were not. Every gate and every shadow view puts its text at the front of the note, the views last, and the note was cut at 300 characters: on the live crypto lane's 142 passes of its first 40 minutes back, all 300 were the crossed, spike-view and scenario readings and not one row said why the buy was turned down.
    A reading in square brackets describes the market; a sentence outside them is a rule's verdict. The verdicts are kept in their order and put first, then the readings, and the whole note is stored. A paper floor bid's mark stays at the very front: paperfill.py looks for it."""
    tags = _TAG.findall(note)
    text = " ".join(_TAG.sub(" ", note).split())
    return " ".join(x for x in (text, " ".join(tags)) if x)[:REASON_MAX]


def _log(store: Store, s: Settings, shot: LongShot, **fields) -> None:
    fields.setdefault("lane_ver", _lane_version(store, s, shot.lane))
    if shot.factors:
        fields.setdefault("factors", shot.factors)
    if shot.factors_job is not None:
        fields["_factors_job"] = shot.factors_job  # the entry snapshot is taken by whoever writes the row, as of the decision's time: the evaluation does not read the tape for it
    store.add_decision(
        ticker=shot.ticker, title=shot.title, bid=shot.bid, ask=shot.ask, mode="trade" if s.is_live(shot.lane, shot.series) else "observe",
        close_time=shot.close_time, lane=shot.lane, category=shot.category,
        event_ticker=shot.event_ticker, **fields,
    )


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
    dry: bool = False,
) -> Outcome:
    """Rate one market, log the decision, and place an order if allowed.

    dry: decide only. No order, no decision row, no shadow row of the lane's own (the lane field buy_redecide_shadow asks for one more decision of a market whose resting buy missed, to record what it would have done).

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
        if not dry:
            _log(
                store, s, shot, model_p=None, edge=None, action="pass",
                reason=f"model error: {exc}"[:200], context=getattr(exc, "context", "")[:1500],
            )
        return Outcome(
            shot.ticker, shot.title, shot.ask, None, None, "pass", f"model error: {exc}", shot.lane
        )

    p, why = side_prob(got.p, shot.side), got.why  # chance the contract we would buy pays
    cal: Calibration = store.calibrator.adjust(shot.lane, shot.category, shot.ask, p, spec.trust)
    adjusted_note = "" if cal.n == 0 else cal.note(p)
    if not spec.record_adjust:
        cal = Calibration(p, 1.0, cal.band_p, cal.band_n, cal.skill, cal.n)  # the lane bets on its estimator's own chance: the adjustment is noted, not applied
    e = edge(cal.p, shot.ask)
    chain = bool(spec.buy_chain)  # the lane's buy module decides, step by step in its order (buychain.py); a lane with none (the old LLM lanes) keeps the edge screen
    bet = True if chain else wants_bet(spec, cal.p, shot.ask, e)
    note = why if not adjusted_note else f"{why} | {adjusted_note}" if spec.record_adjust else f"{why} | record adjustment off for this lane, betting on {p:.2f} (it would have read: {adjusted_note})"
    both = None
    if spec.needs_model_edge:  # both sides of the market from this one chance and this one quote, on the note of buys and passes alike: it shows what the lane's rule made of the side it was not asked about (the decision itself is still the lines above)
        p_yes = cal.p if shot.side == "yes" else 1.0 - cal.p
        yes_bid, yes_ask = (shot.bid, shot.ask) if shot.side == "yes" else (1.0 - shot.ask, 1.0 - shot.bid)
        both = both_sides(spec, p_yes, yes_bid, yes_ask)
        note = f"{both_sides_tag(both)} {note}"
    if not bet:
        note = f"{edge_verdict(spec, cal.p, shot.ask, e) or ''} {note}".strip()  # the edge screen says so in words, as every other rule does
    if chain:
        from . import buychain

        p_yes = cal.p if shot.side == "yes" else 1.0 - cal.p
        bet, chain_note = buychain.decide(buychain.Ctx(s, spec, p_yes, store=store, why=why), shot)
        note = f"{chain_note} {note}"
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
    if shot.series:
        from . import marketprofile as _marketprofile

        mtype = _marketprofile.type_of(shot.series)
        if mtype:
            note = f"[mtype {mtype}] {note}"
    if spec.entry_scenario_view:  # shadow: a reading for the note, on passes as well as buys
        try:
            sc_note = side_scenario(store, shot)
        except Exception as exc:
            sc_note = None
            log.warning("scenario unreadable for %s %s: %s", shot.ticker, shot.side, exc)
        note = f"{sc_note or '[scenario unreadable]'} {note}"
    if spec.entry_spike_view:  # shadow: the note carries the reading on passes as well as buys, so the skipped decisions can be scored too
        try:
            sv = side_spike_view(shot)
        except Exception as exc:
            sv = None
            log.warning("spike view unreadable for %s %s: %s", shot.ticker, shot.side, exc)
        note = f"[spike view z {sv[0]:+.1f}, {sv[1]} of {curverisk.SPIKE_STRETCHES} as large] {note}" if sv is not None else f"[spike view unreadable] {note}"
    stake_factor = 1.0
    sized = Sizing(0, 0.0, "")
    if bet:
        with steptime.step("balance read"):
            balance = store.bankroll.read(client, s, live=s.is_live(shot.lane, shot.series))
        sized = size_bet(
            spec, s, cal.p, shot.ask, balance.available, balance.equity,
            balance.open_by_lane.get(shot.lane, 0.0), stake_factor, fractional=s.fractional_stake and not _NO_FRACTION,
            maker_price=round(max(0.01, shot.ask - spec.buy_rest_below), 4) if spec.buy_rest_seconds > 0 else None,
        )
        if store.holds_sized_bet(shot.ticker, shot.lane) and not s.is_live(shot.lane, shot.series):
            sized = Sizing(0, 0.0, "already holding this market (paper)")
        elif not sized.ok:
            bet = False
            note = f"not sized: {sized.why}. {note}"
        else:
            note = f"{sized.why}. {note}"
    if bet and dry:
        note = f"[dry: a decision to buy, nothing ordered] {note}"
    elif bet and s.is_live(shot.lane, shot.series):
        _MISS.missed = False
        with contextlib.nullcontext() if spec.buy_rest_seconds > 0 else _ORDER_LOCK:  # a resting buy waits for its fill: it must not hold every other lane's order
            placed = place(
                s, client, store, shot, note, sized.contracts,
                order_key=order_key, before_order=before_order,
            )
        note = placed.note
        if spec.buy_redecide_shadow and getattr(_MISS, "missed", False):
            _MISS.missed = False
            _redecide_shadow(s, client, est, store, shot, spec, placed, searcher=searcher, quant=quant, estimators=estimators, share=share)
        if placed.filled > 0:
            # Record what actually filled, not what was asked for: a partial fill is a smaller bet.
            # Keep the fraction. int(1.28) stored a 1.28-contract fill as 1, so the win was booked at $1.
            sized = Sizing(round(placed.filled, 2), placed.cost, sized.why)
        else:
            bet = False  # blocked, refused, failed or unfilled: there is no position to track
    action = "bet" if bet else "pass"
    held = {"contracts": sized.contracts, "stake_usd": sized.cost_usd} if bet and sized.ok else {}
    if dry:
        return Outcome(shot.ticker, shot.title, shot.ask, p, e, action, note, shot.lane)
    if bet and spec.stake_vol_shadow and s.is_live(shot.lane, shot.series):
        _stake_vol_shadow(s, shot, sized)
    _log(
        store, s, shot, model_p=p, edge=e, action=action, reason=stored_reason(note),
        context=got.context[:1500], cal_p=cal.p, side=shot.side, **held,
    )
    return Outcome(shot.ticker, shot.title, shot.ask, p, e, action, note, shot.lane)



def _redecide_shadow(s: Settings, client: KalshiClient, est, store: Store, shot: LongShot, spec: LaneSpec, placed: Placed, **kw: Any) -> None:
    """A live resting buy rested its time and filled nothing (lane field buy_redecide_shadow; user, 2026-11-18: "shouldn't another tick have gone by with another choice?"): decide the market once more as it is NOW, with every gate and no order and no decision row (evaluate(dry=True)), and write what it would have done beside what the live lane did, as one row of kind "redecide" in data/risk_shadow.jsonl. It runs after the live lane's own order flow is over, so it cannot hold up a buy, and it never raises: whatever fails is logged once as a warning and the buy goes on as it was."""
    import json
    from dataclasses import replace as _replace

    try:
        book = client.orderbook(shot.ticker)
        ask_now = best_ask(book, shot.side)
        bid_now = best_bid(book, shot.side)
        row: dict[str, Any] = {
            "ts": round(time.time(), 2), "kind": "redecide", "lane": shot.lane, "ticker": shot.ticker, "side": shot.side, "live": s.is_live(shot.lane, shot.series),
            "decided_ask": shot.ask, "rest_price": round(max(0.01, shot.ask - spec.buy_rest_below), 4), "ask_now": ask_now, "bid_now": bid_now,
            "live_filled": round(placed.filled, 4), "live_cost": round(placed.cost, 4), "live_price": round(placed.cost / placed.filled, 4) if placed.filled > 0 else None,
        }
        if ask_now is None:
            row.update(would="none", why="the book has no ask")
        else:
            fresh = _replace(shot, ask=ask_now, bid=bid_now if bid_now is not None else shot.bid, factors="", factors_job=None)
            out = evaluate(s, client, est, store, fresh, kw.get("searcher"), quant=kw.get("quant"), estimators=kw.get("estimators"), share=kw.get("share"), dry=True)
            row.update(would="bet" if out.action == "bet" else "pass", would_rest=round(max(0.01, ask_now - spec.buy_rest_below), 4) if out.action == "bet" else None, why=out.note[:300])
        with open(Path(s.data_dir) / "risk_shadow.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as exc:
        log.warning("redecide shadow failed on %s %s and is passed over: %r", shot.ticker, shot.lane, exc)



def _stake_vol_shadow(s: Settings, shot: LongShot, sized: Sizing) -> None:
    """One row of kind "stake_vol" in data/risk_shadow.jsonl for a live bet on a lane with stake_vol_shadow (user, 2026-11-18: "scale the bet by lane volatility too"; "Yes, shadow it"): the market's own typical minute at the buy, the price and the stake that was actually used. Memory only (the quote mids already held), changes no stake, never raises: a reading that cannot be made writes no row."""
    from . import quoteshock
    from . import ticks as ticks_mod

    try:
        now = time.time()
        unit = quoteshock.typical_minute(ticks_mod.mids.rows(shot.ticker), now)
        if unit is None or unit <= 0:
            return
        row = {"ts": round(now, 2), "kind": "stake_vol", "lane": shot.lane, "ticker": shot.ticker, "side": shot.side, "live": True, "ask": shot.ask, "stake": round(float(sized.cost_usd), 4), "contracts": round(float(sized.contracts), 4), "unit": round(float(unit), 5), "left": round(shot.hours_to_close * 3600.0, 1)}
        with open(Path(s.data_dir) / "risk_shadow.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as exc:
        log.warning("stake_vol shadow failed on %s %s and is passed over: %r", shot.ticker, shot.lane, exc)


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


def _load_quotes(s: Settings, ticker: str, start: float, end: float) -> list[tuple[float, float, float]]:
    """Quotes with start < ts <= end, oldest first. A market being watched answers from memory. Otherwise the 60s snapshot, and the database only when this market is not watched."""
    from . import db as database
    from . import tape as tape_mod

    if tape_mod.recent.stop_armed(ticker):
        got = tape_mod.recent.stop_quotes(ticker, start, end)
        if got is None:
            got = tape_mod.recent.quote_tail(ticker, start, end)
        return [(ts, bid, ask) for ts, bid, ask in got if bid > 0 and ask > 0]
    got = tape_mod.recent.quotes(ticker, start, end)
    if got is not None:
        return [(ts, bid, ask) for ts, bid, ask in got if bid > 0 and ask > 0]
    rows: list[tuple[float, float, float]] = []
    try:
        tape = database.connect(s.tape_path, "tape", shared=True)
        rows = [(float(ts), float(bid), float(ask)) for ts, bid, ask in tape.execute(
            "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts<=? AND ts>? AND bid > 0 AND ask > 0 ORDER BY ts",
            (ticker, end, start),
        )]
    except Exception:
        rows = []
    extra = [(ts, bid, ask) for ts, bid, ask in tape_mod.recent.quote_tail(ticker, start, end) if bid > 0 and ask > 0]
    if not extra:
        return rows
    by = {row[0]: row for row in rows}
    for row in extra:
        by[row[0]] = row
    return [by[k] for k in sorted(by)]


def arm_stop_history(s: Settings, ticker: str, now: float | None = None) -> None:
    """Load the last 15 minutes of this market's quotes once, when a watch of it starts. Later quotes extend that copy; the stop reads it instead of the database."""
    from . import db as database
    from . import tape as tape_mod

    if tape_mod.recent.stop_armed(ticker):
        return
    now = time.time() if now is None else now
    if not database.tape_exists(s.tape_path):
        return
    start = now - tape_mod.STOP_SECONDS
    try:
        tape = database.connect(s.tape_path, "tape", shared=True)
        rows = [(float(ts), float(bid), float(ask)) for ts, bid, ask in tape.execute(
            "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts<=? AND ts>? AND bid > 0 AND ask > 0 ORDER BY ts",
            (ticker, now, start),
        )]
    except Exception:
        return
    tape_mod.recent.arm_stop(ticker, start, rows)


def arm_feed_history(s: Settings, symbol: str, now: float | None = None) -> None:
    """Load the last 15 minutes of one spot price once, when a watch that might compare it starts."""
    from . import db as database
    from . import tape as tape_mod

    if tape_mod.recent.feed_armed(symbol):
        return
    now = time.time() if now is None else now
    if not database.tape_exists(s.tape_path):
        return
    start = now - tape_mod.STOP_SECONDS
    try:
        tape = database.connect(s.tape_path, "tape", shared=True)
        rows = [(float(ts), float(px)) for ts, px in tape.execute(
            "SELECT ts, px FROM feed_ticks WHERE symbol=? AND ts<=? AND ts>? ORDER BY ts", (symbol, now, start),
        )]
    except Exception:
        return
    tape_mod.recent.arm_feed(symbol, start, rows)


def arm_index_history(s: Settings, index_id: str, now: float | None = None) -> None:
    """Load the last 31 minutes of a settlement index once. The last-minute chance then reads the copy."""
    from . import db as database
    from . import tape as tape_mod

    if tape_mod.recent.index_armed(index_id):
        return
    now = time.time() if now is None else now
    if not database.tape_exists(s.tape_path):
        return
    start = now - tape_mod.INDEX_SECONDS
    try:
        tape = database.connect(s.tape_path, "tape", shared=True)
        rows = [(float(ts), float(value)) for ts, value in tape.execute(
            "SELECT ts, value FROM index_ticks WHERE index_id=? AND ts<=? AND ts>? AND value > 0 ORDER BY ts",
            (index_id, now, start),
        )]
    except Exception:
        return
    tape_mod.recent.arm_index(index_id, start, rows)


def side_trend(s: Settings, shot: LongShot, seconds: float, now: float | None = None) -> float | None:
    """How far the price of the side we would buy (the mid of its bid and ask, in its own terms) has moved over the last `seconds`, from the tape's quotes. None when the tape has no quote that far back or none lately. A lane that asks for this check treats None as no buy: it has nothing to compare."""
    from . import tape as tape_mod

    now = time.time() if now is None else now
    last = tape_mod.recent.latest_quote(shot.ticker, now - 30.0, now)
    then = tape_mod.recent.latest_quote(shot.ticker, now - seconds - 60.0, now - seconds)
    if last is tape_mod.MISS:
        last_rows = _load_quotes(s, shot.ticker, now - 30.0, now)
        last = last_rows[-1] if last_rows else None
    if then is tape_mod.MISS:
        then_rows = _load_quotes(s, shot.ticker, now - seconds - 60.0, now - seconds)
        then = then_rows[-1] if then_rows else None
    if last is None or then is None:
        return None

    def price(row: Any) -> float:
        mid = (row[1] + row[2]) / 2.0
        return mid if shot.side == "yes" else 1.0 - mid

    return price(last) - price(then)


def side_spike(s: Settings, shot: LongShot, seconds: float, now: float | None = None) -> tuple[float | None, float | None]:
    """(how far the side's mid has risen over the last `seconds`, how far its bid swung inside them), from the tape's quotes. None for either when the tape has no quote to say."""
    now = time.time() if now is None else now
    rows = _load_quotes(s, shot.ticker, now - seconds - 60.0, now)
    if not rows or now - rows[-1][0] > 30.0:
        return None, None

    def mid(r: Any) -> float:
        m = (r[1] + r[2]) / 2.0
        return m if shot.side == "yes" else 1.0 - m

    def bid(r: Any) -> float:
        return r[1] if shot.side == "yes" else 1.0 - r[2]

    back = [r for r in rows if r[0] <= now - seconds]
    inside = [bid(r) for r in rows if r[0] >= now - seconds]
    rose = mid(rows[-1]) - mid(back[-1]) if back else None
    return rose, (max(inside) - min(inside) if inside else None)


SPOT_STALE = 30.0  # a spot price older than this says nothing about now
SPOT_LOOKBACK = 40.0  # seconds of spot prices read to find the last changes


def _spot_symbols(series: str) -> list[str]:
    """The one underlying price a decision reads. Crypto uses Coinbase. Anything else uses its own instrument."""
    from .commodities import contract_priced, symbol_for

    inst = symbol_for(series)
    if not inst or contract_priced(inst) or inst.startswith("kp:"):  # a Kalshi perp reference is an index on the tape, not a feed_ticks symbol
        return []
    return [inst]


def side_spike_view(shot: LongShot, now: float | None = None) -> tuple[float, int] | None:
    """The 3-hour spike reading for the buy's note (lane field entry_spike_view): curverisk.spike_view on the series' Kalshi perp. None when the series has no perp or the 3 hours are not in memory."""
    from . import ticks as ticks_mod
    from .watcher import perp_index

    now = time.time() if now is None else now
    kp = perp_index(shot.series or shot.ticker.split("-")[0])
    if not kp:
        return None
    got = ticks_mod.history.window(kp, now - curverisk.SPIKE_LOOK * (curverisk.SPIKE_STRETCHES + 2) - 130.0, now)
    return curverisk.spike_view(got[0], got[1], now, shot.side) if got else None


def side_scenario(store: Store, shot: LongShot, now: float | None = None) -> str | None:
    """The market's scenario as a tag for the note (lane field entry_scenario_view; scenario.py): the target and its place in the 15, 30 and 60 minute scopes before the open, the cushion and the spot's place in each scope now, and the series' last four markets. None when the series has no spot index, the hour before the open is not in memory, or the target cannot be read."""
    import bisect

    from . import scenario, variants
    from . import ticks as ticks_mod

    now = time.time() if now is None else now
    series = shot.series or shot.ticker.split("-")[0]
    left = shot.hours_to_close * 3600.0
    opened = now + left - 900.0  # 900: a 15-minute market's length, structural
    ix = variants.spot_index(series, "kalshi", now)  # Kalshi's own price, as riskscore.gather (2026-11-18)
    got = ticks_mod.history.window(ix, opened - 3700.0, now) if ix else None
    if not got:
        return None
    ts, vs = got
    opening = scenario.at_open(ts, vs, opened)
    if opening is None:
        return None
    sc = opening["scopes"]
    unit = next((sc[m]["unit"] for m in reversed(scenario.SCOPES) if m in sc), None)  # the widest scope read
    cush = scenario.cushion(ts, vs, now, opening["target"], shot.side, left, unit) if unit else None
    i = bisect.bisect_right(ts, now) - 1
    now_place = {m: got_["place"] for m in scenario.SCOPES if i >= 0 and (got_ := scenario.scope(ts, vs, now, m, vs[i])) is not None}
    from . import riskscore

    prev = riskscore.previous(ts, vs, ticks_mod.mids.series_rows(series, opened, 3600.0), opened, unit)  # from memory, no database read
    return scenario.note(opening, cush, now_place, prev)


def side_view(shot: LongShot, scales: tuple[int, ...], now: float | None = None) -> float | None:
    """The buy's view (lane field entry_view_scales): the chance the side pays on the long layer's own reading, the Kalshi perp's movement read at `scales` minutes together (curverisk.long_chance; 0 = the market so far), the cushion measured from the perp at the window's open. None when the series has no perp, no tick is fresh or the window's open was not recorded."""
    from . import ticks as ticks_mod
    from .watcher import perp_index

    now = time.time() if now is None else now
    kp = perp_index(shot.series or shot.ticker.split("-")[0])
    if not kp:
        return None
    left = shot.hours_to_close * 3600.0
    reach = 60.0 * max(max(scales), 1) + 100.0
    got = ticks_mod.history.window(kp, now - max(3700.0, reach), now)
    ref = ticks_mod.history.first_at_or_after(kp, now + left - 900.0, 60.0)
    if not got or not ref:
        return None
    return curverisk.long_chance(got[0], got[1], now, ref, shot.side, left, scales=tuple(scales), elapsed=max(0.0, 900.0 - left))


_risk_marks: dict[tuple[str, tuple[int, ...]], list[tuple[float, float | None, float | None, float | None]]] = {}  # (ticker, long scales) -> (mark time, YES bid, YES ask, the long layer's YES chance): a past mark does not change, so each is read once
_risk_seen: dict[str, deque[tuple[float, float]]] = {}  # series -> (time, reading) of the last hour: the range an anomaly is judged against
_risk_seen_at: dict[tuple[str, str], float] = {}  # (ticker, side) -> when its last reading was kept (one per mark)


def risk_marks(shot: LongShot, scales: tuple[int, ...], now: float) -> list[tuple[float, float | None, float | None, float | None]]:
    """The market's marks for the risk trend, every curverisk.TREND_STEP seconds from its open to `now`, from memory alone: Kalshi's YES bid and ask at the mark (the last quote, at most 30 s old) and the long layer's chance that YES pays as it stood then (curverisk.long_chance on the Kalshi perp, read only from ticks at or before the mark). Either is None where it could not be read."""
    from . import ticks as ticks_mod
    from .watcher import perp_index

    step = curverisk.TREND_STEP
    close = now + shot.hours_to_close * 3600.0
    opened = close - 900.0  # 900: a 15-minute market's length, structural
    key = (shot.ticker, tuple(scales))
    marks = _risk_marks.get(key)
    if marks is None:
        if len(_risk_marks) > 400:
            _risk_marks.clear()
        marks = _risk_marks[key] = []
    t = (marks[-1][0] + step) if marks else math.ceil(opened / step) * step  # marks sit on the clock's own 5 s grid, so a close time read a second off between calls does not move them
    if t > now:
        return marks
    quotes = ticks_mod.mids.quotes(shot.ticker)
    qt = [q[0] for q in quotes]
    kp = perp_index(shot.series or shot.ticker.split("-")[0])
    reach = max(3700.0, 60.0 * max(max(scales, default=60), 1) + 100.0)
    got = (ticks_mod.history.window(kp, now - reach - 900.0, now) or ticks_mod.history.window(kp, now - reach, now)) if kp else None
    ref = ticks_mod.history.first_at_or_after(kp, opened, 60.0) if kp else None
    while t <= now:
        i = bisect.bisect_right(qt, t) - 1
        bid, ask = (quotes[i][1], quotes[i][2]) if i >= 0 and t - quotes[i][0] <= 30.0 else (None, None)
        long_yes = curverisk.long_chance(got[0], got[1], t, ref, "yes", close - t, scales=tuple(scales), elapsed=max(0.0, t - opened)) if got and ref and close > t else None
        marks.append((t, bid, ask, long_yes))
        t += step
    return marks


def risk_trend_read(shot: LongShot, spec: LaneSpec, now: float | None = None, keep: bool = True) -> tuple[float, float, str] | None:
    """The buy's risk trend (lane field entry_risk_trend): (the move of the side's risk over the look back in the market's own typical moves, the look back in seconds, "holding" / "worsening" / "anomaly"). The risk is the exit's own number for the side at each mark: curverisk.risk on the bid net of the sell fee and the long layer's chance. The look back is the lane's share of the time left. None when it cannot be read. Memory only."""
    from .strategy import fee_per_contract

    now = time.time() if now is None else now
    left = shot.hours_to_close * 3600.0
    back = spec.entry_risk_trend * left
    yes = shot.side == "yes"
    qs: list[tuple[float, float | None]] = []
    for t, bid, ask, long_yes in risk_marks(shot, tuple(spec.curve_risk_long_scales), now):
        side_bid = (bid if yes else 1.0 - ask) if bid is not None and ask is not None else None
        if side_bid is None or long_yes is None or not 0.0 < side_bid < 1.0:
            qs.append((t, None))
            continue
        qs.append((t, curverisk.risk(long_yes if yes else 1.0 - long_yes, side_bid - fee_per_contract(side_bid), 1.0, curverisk.LONG_WEIGHT).q))
    got = curverisk.risk_trend(qs, now, back)
    if got is None:
        return None
    z = got[0] / got[1]
    seen = _risk_seen.setdefault(shot.series or shot.ticker.split("-")[0], deque())
    while seen and seen[0][0] < now - 3600.0:  # the current hour: the range a reading is judged against
        seen.popleft()
    state = curverisk.trend_state(z, [v for _, v in seen])
    if len(_risk_seen_at) > 2000:
        _risk_seen_at.clear()
    if keep and now - _risk_seen_at.get((shot.ticker, shot.side), 0.0) >= curverisk.TREND_STEP:  # keep=False: a second look at the other side for the note, not added to the hour's range
        _risk_seen_at[(shot.ticker, shot.side)] = now
        seen.append((now, z))
    return z, back, state


def side_trend_read(s: Settings, shot: LongShot, seconds: float, now: float | None = None) -> tuple[float, float] | None:
    """The side's price move over the last `seconds` and that market's own noise for the same span (the standard deviation of its `seconds`-long changes over the 2 minutes before), both in probability. None when the tape has too little to say."""
    import bisect
    import statistics

    now = time.time() if now is None else now
    window = max(120.0, 2.0 * seconds)  # 2026-11-14: the noise is read over twice the span (2 minutes at the least), so a look back of three minutes can be read; up to 60 s this is the 2 minutes it always was
    q = _load_quotes(s, shot.ticker, now - window - 30.0, now)
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
    n = int(window - seconds)
    grid = [at(now - 5.0 - k) for k in range(int(window))]
    steps = [a - b for a, b in zip(grid[:n], grid[int(seconds):int(seconds) + n], strict=False) if a is not None and b is not None]
    if len(steps) < 20:
        return None
    return cur - then, statistics.pstdev(steps)


def side_trend_z(s: Settings, shot: LongShot, seconds: float, now: float | None = None) -> float | None:
    """The side's price move over the last `seconds` in units of that market's own noise: the move divided by the standard deviation of its `seconds`-long changes over the 2 minutes before. None when the tape has too little to say or the market has not moved."""
    got = side_trend_read(s, shot, seconds, now)
    if got is None:
        return None
    return z_of(*got)


def z_of(move: float, sd: float) -> float:
    """A move in units of the market's own noise (side_trend_z's rule)."""
    if sd <= 1e-6:  # a market whose price never changed lately: no move is no gain, a move is a big one
        return 0.0 if abs(move) < 1e-9 else math.copysign(10.0, move)
    return move / sd


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


_MISS = threading.local()  # set by place() on this thread when a resting bid rested its time and filled nothing, read by evaluate() right after (lane field buy_redecide_shadow)


_LANE_LOCKS: dict[str, threading.Lock] = {}
_LANE_LOCKS_GUARD = threading.Lock()
_ORDER_LOCK = threading.Lock()  # the risk check, the order and its ledger row happen as one step for an immediate buy: two workers cannot both pass a global cap and then both buy


def lane_lock(lane: str) -> threading.Lock:
    """The lock one lane's evaluations take, so buy workers can run different lanes at the same time without two of one lane racing past its own limits."""
    with _LANE_LOCKS_GUARD:
        return _LANE_LOCKS.setdefault(lane, threading.Lock())


_NO_FRACTION: set[str] = set()  # filled once Kalshi has refused a fractional order that the whole-contract retry then got through: stop asking for fractions


def _cap_equity(s: Settings, client: KalshiClient, store: Store) -> float:
    """The account's equity, read only when a cap is set as a share of it (MAX_TRADE_PCT, MAX_LOSS_PCT_PER_DAY); the bankroll read is cached, so this is not a new request each buy."""
    if s.max_trade_pct <= 0 and s.max_loss_pct_per_day <= 0:
        return 0.0
    try:
        return float(store.bankroll.read(client, s).equity)
    except Exception:
        return 0.0


TICK_CHASE_MAX = 0.02  # dollars over decision ask + buy_slip that a tick-ahead limit may reach (set by hand, not from a study)


def side_ask_ticks(ticker: str, side: str, now: float | None = None, span: float = 30.0) -> tuple[float, float] | None:
    """(the newest ask of the side from the live quote stream, the signed change between its last two distinct values) over the last `span` seconds, or None when the stream has none. YES ask = the quote's ask, NO ask = 1 - the YES bid."""
    from . import tape as tape_mod

    now = time.time() if now is None else now
    rows = tape_mod.recent.quote_tail(ticker, now - span, now + 1.0)
    asks: list[float] = []
    for row in rows:
        if row[1] <= 0 or row[2] <= 0:
            continue
        a = round(float(row[2]) if side == "yes" else 1.0 - float(row[1]), 4)  # the exchange's scale
        if not asks or abs(a - asks[-1]) > 1e-9:
            asks.append(a)
    if not asks:
        return None
    return asks[-1], round(asks[-1] - asks[-2], 4) if len(asks) >= 2 else 0.0


def buy_limit(spec: LaneSpec | None, ticker: str, side: str, decision_ask: float, base_ask: float, now: float | None = None) -> float:
    """The limit of an immediate buy. The old rule: the ask the order is built from plus buy_slip. With buy_tick_ahead: the newest streamed ask (or `base_ask`, whichever is higher when the stream is silent or behind the book) plus the last tick up, capped at the decision ask + buy_slip + buy_retry_chase; a falling ask is bought at its new level, never over the cap."""
    slip = max(0.0, spec.buy_slip) if spec is not None else 0.0
    if spec is None or not getattr(spec, "buy_tick_ahead", False):
        return round(max(base_ask, min(0.99, base_ask + slip)), 4)
    got = side_ask_ticks(ticker, side, now)
    cap = decision_ask + slip + min(max(0.0, spec.buy_retry_chase), TICK_CHASE_MAX)  # the most the lane will pay: an ask that has run over it is not bought (the order then finds nothing offered and the buy is skipped). The chase part is held to TICK_CHASE_MAX whatever the lane's retry chase: a favourite's edge is a cent or two
    if got is None:
        return round(min(0.99, cap, base_ask + slip), 4)
    fresh, delta = got
    base = fresh if fresh < base_ask - 1e-9 and delta < 0 else max(fresh, base_ask)  # a falling ask is bought where it is now
    return round(min(0.99, cap, base + max(delta, 0.0)), 4)


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
    limit = buy_limit(lane_spec, shot.ticker, shot.side, shot.ask, shot.ask)  # four decimals, the exchange's scale: rounding to cents could land under a sub-cent ask; never under the ask
    if lane_spec is not None and getattr(lane_spec, "buy_tick_ahead", False):
        seen = side_ask_ticks(shot.ticker, shot.side)
        log.info("buy limit %s %s %s: decided at %.2f, the stream's ask %s (last tick %s), limit %.4f", shot.lane, shot.ticker, shot.side, shot.ask, f"{seen[0]:.2f}" if seen else "silent", f"{seen[1]:+.2f}" if seen else "-", limit)
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
    need = s.min_offer_ratio * cost if s.min_offer_ratio > 0 else s.min_order_usd  # a share of our own order when the ratio is set, else the old flat dollars
    if offered + 1e-9 < need:
        now_ask = best_ask(book, shot.side)
        moved = f"; the {shot.side} ask is now {now_ask:.2f}, over the {limit:.2f} limit (it was {shot.ask:.2f} when the lane decided): the price ran away while the order waited" if now_ask is not None and now_ask > limit + 1e-9 else ""
        return Placed(
            f"skipped: ${offered:.2f} offered at {limit:.2f}, "
            f"under the ${need:.2f} needed ({s.min_offer_ratio:.0%} of the ${cost:.2f} order){moved}" if s.min_offer_ratio > 0 else
            f"skipped: ${offered:.2f} offered at {limit:.2f}, "
            f"under the ${s.min_order_usd:.2f} minimum{moved}"
        )
    with steptime.step("risk check"):
        verdict = risk.check(
            s, store, shot.ticker, cost, max_trade=cap,
            event_ticker=shot.event_ticker, mutually_exclusive=shot.mutually_exclusive, equity=_cap_equity(s, client, store),
        )
    if not verdict.ok:
        return Placed(f"blocked by risk: {verdict.reason}")
    with steptime.step("top up"):
        topped = _top_up(s, client, store, shot, cost)
    if topped:
        return Placed(topped)
    known = _EXCHANGE_OF.get(shot.series)  # filled only when a Kalshi refusal named the exchange, so another venue's series never matches
    if known is not None and _UNFUNDED.get(known, 0.0) > time.time():
        return Placed(f"skipped: exchange {known} (where {shot.series} trades) has no cash; move funds there in the Kalshi app")
    if before_order:
        before_order()
    if lane_spec is not None and lane_spec.buy_rest_seconds > 0:
        placed = _place_resting(s, client, store, shot, why, count, lane_spec, order_key, book)
        if 0 < placed.filled < count - 1e-9 and lane_spec.buy_rest_then_take and lane_spec.buy_rest_top_up:
            placed = _top_up_rest(s, client, store, shot, lane_spec, placed, count, order_key)
        _MISS.missed = placed.filled <= 0 and "did not fill" in placed.note  # a bid that rested its time and filled nothing
        if placed.filled > 0 or not lane_spec.buy_rest_then_take or "did not fill" not in placed.note:
            return placed  # a refusal or a skip is not a miss: only a bid that rested its time and filled nothing falls back to the ask
        again = _requote(s, client, store, shot, lane_spec, count)
        if again is None:
            return Placed(f"{placed.note} Second try at the ask not placed: {GAVE_UP.get((shot.ticker, shot.side)) or 'no reason kept'}.", placed.filled, placed.cost, placed.price)
        log.info("buy %s %s: the resting bid did not fill, taking the ask at %.2f", shot.ticker, shot.side, again)
        limit = again
        order_key = f"{order_key}-take" if order_key else None  # a new id: Kalshi refuses one it has seen, even on an order that filled nothing
    buy = client.buy_yes_ioc if shot.side == "yes" else client.buy_no_ioc
    retries = lane_spec.buy_retries if lane_spec is not None else 0
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
        if filled > 0 or lane_spec is None or attempt >= retries:
            break
        again = _requote(s, client, store, shot, lane_spec, count)
        if again is None:
            break
        attempt += 1
        limit = again
        key = f"{order_key}-r{attempt}" if order_key else None  # a new id: Kalshi refuses one it has seen, even on an order that filled nothing
        log.info("buy %s %s: nothing filled, trying again at %.2f (try %d of %d)", shot.ticker, shot.side, limit, attempt + 1, retries + 1)
    return Placed(f"bought {shot.side.upper()} {filled:g}/{count} @ {avg:.4f} ({s.env}). {why}", filled, spent)


def _top_up_rest(s: Settings, client: KalshiClient, store: Store, shot: LongShot, spec: LaneSpec, placed: Placed, count: float, order_key: str | None) -> Placed:
    """Buy what a resting bid left unfilled at the fresh ask, under the same guards as the fallback for a bid that filled nothing (price band, buy_retry_chase, the risk gate). If any guard refuses or the order fails,
    the part that did fill stands as it was. The merged result carries the blended price, so the take-profit and the log read the whole position."""
    left = count - placed.filled
    limit = _requote(s, client, store, shot, spec, left, topping_up=True)
    if limit is None or left * limit < s.min_order_usd:
        return Placed(f"{placed.note} (the other {left:g} not topped up: ask moved out of the lane's guards or under the minimum)", placed.filled, placed.cost, placed.price)
    buy = client.buy_yes_ioc if shot.side == "yes" else client.buy_no_ioc
    try:
        with steptime.step("order"):
            resp = buy(shot.ticker, left, limit, client_order_id=f"{order_key}-top" if order_key else None)
    except KalshiRateLimited:
        raise
    except KalshiError as exc:
        log.warning("top-up of %s %s failed: %s", shot.ticker, shot.side, exc)
        return placed
    more = float(resp.get("fill_count") or 0.0)
    avg = float(resp.get("average_fill_price") or limit)
    fee = float(resp.get("average_fee_paid") or 0.0) * more
    store.add_order(ticker=shot.ticker, env=s.env, count=left, price=limit, fill_count=more, cost_usd=avg * more + fee, fee_usd=fee, order_id=resp.get("order_id", ""), contract=shot.side)
    if more <= 0:
        return placed
    spent = placed.cost + avg * more + fee
    blended = (placed.price * placed.filled + avg * more) / (placed.filled + more) if placed.price else avg
    log.info("buy %s %s: the resting bid filled %g of %g, topped up %g at %.2f", shot.ticker, shot.side, placed.filled, count, more, avg)
    return Placed(f"{placed.note} Topped up {more:g} more at {avg:.4f}, fee ${fee:.4f}.", placed.filled + more, spent, blended)


GAVE_UP: dict[tuple[str, str], str] = {}  # why the last second try on a market and side was not placed, for the decision note


def _requote(s: Settings, client: KalshiClient, store: Store, shot: LongShot, spec: LaneSpec, count: float, topping_up: bool = False) -> float | None:
    """The limit for a second try, or None to give up (the reason is kept in GAVE_UP for the note); see _requote_why."""
    limit, why = _requote_why(s, client, store, shot, spec, count, topping_up)
    GAVE_UP[(shot.ticker, shot.side)] = why
    return limit


def _requote_why(s: Settings, client: KalshiClient, store: Store, shot: LongShot, spec: LaneSpec, count: float, topping_up: bool = False) -> tuple[float | None, str]:
    """(The limit for a second try after an immediate buy filled nothing, or None to give up, and why it gave up (2026-11-15 night, user: several buy orders did not fill; the container log is lost at every rebuild, so the reason goes into the stored note). It reads the book again and prices the order off the fresh ask, but never above the first limit plus
    `buy_retry_chase`, never outside the lane's price band, and only if the curve (model or record) and the risk gate still agree at the limit, the most the order can pay: a market that ran away is not chased."""
    try:
        ask = best_ask(client.orderbook(shot.ticker), shot.side)
    except KalshiError:
        return None, "the book could not be read"
    if ask is None or ask < spec.price[0] - 1e-9 or ask > spec.price[1] + 1e-9:
        return None, ("the book has no ask" if ask is None else f"the ask {ask:.2f} is outside the lane's price band")
    if ask > shot.ask + max(0.0, spec.buy_retry_chase) + 1e-9:  # a market that ran away is not chased
        return None, f"the ask ran from {shot.ask:.2f} to {ask:.2f}, more than the chase of {spec.buy_retry_chase:g}"
    limit = buy_limit(spec, shot.ticker, shot.side, shot.ask, ask)
    if getattr(spec, "buy_tick_ahead", False):
        seen = side_ask_ticks(shot.ticker, shot.side)
        log.info("buy limit %s %s %s (the take after a resting bid): decided at %.2f, the book's ask %.2f, the stream's ask %s (last tick %s), limit %.4f", shot.lane, shot.ticker, shot.side, shot.ask, ask, f"{seen[0]:.2f}" if seen else "silent", f"{seen[1]:+.2f}" if seen else "-", limit)
    if limit > shot.ask + max(0.0, spec.buy_slip) + max(0.0, spec.buy_retry_chase) + 1e-9:
        return None, f"the limit {limit:.2f} is over the decided ask {shot.ask:.2f} plus the slip and the chase"
    cost = limit * count + order_fee(limit, count)
    verdict = risk.check(s, store, shot.ticker, cost, max_trade=cost, event_ticker=shot.event_ticker, mutually_exclusive=shot.mutually_exclusive, equity=_cap_equity(s, client, store), allow_held=topping_up)  # a top-up's first part is already a position: the holding check would refuse every one
    if not verdict.ok:
        log.info("buy %s %s: %s at %.2f not placed: the risk gate refused (%s)", shot.ticker, shot.side, "the top-up" if topping_up else "the second try", limit, verdict.reason)
    return (limit, "") if verdict.ok else (None, f"the risk gate refused ({verdict.reason})")


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


def _place_resting(s: Settings, client: KalshiClient, store: Store, shot: LongShot, why: str, count: float, spec: LaneSpec, order_key: str | None, book: dict[str, Any] | None = None, price: float | None = None, seconds: float | None = None, hold_price: bool = False) -> Placed:
    """Rest a post-only buy `spec.buy_rest_below` under the ask for `spec.buy_rest_seconds`, then report what filled. Nothing is bought if it does not fill. A resting order that fills is charged no taker fee, and the fee Kalshi charged is recorded as it came.

    `price` and `seconds` fix both, for a bid that must stay at one price (the lane's floor). `hold_price` refuses a cross instead of walking that price down."""
    from . import account

    wait = spec.buy_rest_seconds if seconds is None else seconds
    if price is None:
        price, skip = _resting_price(shot, spec, book)
        if price is None:
            return Placed(skip)
    started = time.time()
    try:
        resp = client.buy_resting(shot.ticker, shot.side, count, price, wait, client_order_id=order_key)
    except KalshiRateLimited:
        raise
    except KalshiError as exc:
        if "post only cross" not in str(exc) or hold_price:
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
            resp = client.buy_resting(shot.ticker, shot.side, count, price, wait, client_order_id=f"{order_key}-retry" if order_key else None)
        except KalshiRateLimited:
            raise
        except KalshiError as exc2:
            return Placed(f"resting buy refused: {exc2}")
    order_id = resp.get("order_id") or (resp.get("order") or {}).get("order_id") or ""
    filled = spent = fee_paid = 0.0
    seen: set[str] = set()
    deadline = started + wait + 3.0
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
        return Placed(f"resting buy at {price:.2f} for {wait:.0f}s did not fill (the ask was {shot.ask:.2f}); nothing bought.")
    return Placed(f"bought {shot.side.upper()} {filled:g}/{count} resting at {price:.2f}, fee ${fee_paid:.4f}. {why}", filled, spent, price)


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


def profit_floor(paid: float) -> float:
    """The lowest limit, in whole cents, at which a sale still clears what the bet cost: L - fee(L) >= paid + fee(paid). The watcher's veto uses the same two fees."""
    from .strategy import fee_per_contract

    need = paid + fee_per_contract(paid)
    cents = max(1, math.ceil(need * 100 - 1e-9))
    while cents < 100 and cents / 100 - fee_per_contract(cents / 100) < need - 1e-9:
        cents += 1
    return cents / 100


def is_profit_sale(why: str) -> bool:
    """True for a sale that cashes a gain (the old take-profit and the curve's profit take). It is sent once at the bid and never chased down the book like a crash exit, and its limit never goes under what the bet cost."""
    return why.startswith(("take-profit", "curve risk exit (profit take)"))


PROFIT_RETRY_SLIP = (0.0,)  # a profit sale that misses is retried once, at the bid and no lower
EXIT_RETRY_SLIP = (0.02, 0.05, 0.10)  # if the first sale fills nothing (or part), sell the rest this far under a fresh read of the best bid, three more times
EXIT_SLIP = 0.02  # an exit order is limited this far under the bid we saw: it is immediate-or-cancel, so it still fills at the best bid


HAND_SALE_MIN_AGE = 20.0  # seconds after the bot's last order on a market before a smaller account position is read as a hand sale (the positions read can trail a fill)


def account_count(client: Any, ticker: str, side: str) -> float | None:
    """Contracts of `side` the account really holds on `ticker` (0 when it holds none or the other side), or None when the account cannot be read."""
    reader = getattr(client, "positions", None)
    if reader is None:
        return None
    try:
        body = reader()
    except KalshiError:
        return None
    from . import manual

    for pos in manual.parse_positions(body):
        if pos.ticker == ticker:
            return pos.count if pos.side == side else 0.0
    return 0.0


def book_hand_sale(s: Settings, store: Store, ticker: str, side: str, real: float, bid: float) -> float:
    """Take off the bot's books the contracts the account no longer holds (sold by hand, or netted by a hand buy of the other side). Returns how many were written off, 0 when the books already agree or the bot's last order is too fresh to trust the read. The written-off contracts are booked at `bid`: the real fill is the user's and is not known here."""
    held = store.net_contracts(ticker, s.env)
    gone = round(held - max(real, 0.0), 2)
    if gone < 0.05 or time.time() - store.last_order_ts(ticker, s.env) < HAND_SALE_MIN_AGE:
        return 0.0
    store.add_order(ticker=ticker, env=s.env, count=gone, price=bid, fill_count=gone, cost_usd=-(bid * gone), fee_usd=0.0, order_id="hand-sale", side="sell", contract=side)
    log.warning("hand sale seen on %s: the books held %.2f %s, the account holds %.2f; wrote off %.2f at the bid %.2f", ticker, held, side, real, gone, bid)
    return gone


def close_by_hand(s: Settings, store: Store, row: Any, real: float, bid: float) -> str | None:
    """A bet the account no longer holds in full: write the missing contracts off the books and, when nothing is left, close the bet so the lane may buy the market again. Returns a log line, or None when nothing changed."""
    with _EXIT_LOCK:
        lane = row["lane"] or "moonshot"
        side = row["side"] or "yes"
        gone = book_hand_sale(s, store, row["ticker"], side, real, bid)
        if gone <= 0:
            return None
        if store.net_contracts(row["ticker"], s.env) < 0.005:
            store.mark_exited(row["ticker"], bid, f"closed by hand: the account no longer holds these contracts (booked at the bid {bid:.2f} when the bot noticed, not at your fill)", lane, side=side)
            return f"{row['ticker']}: closed by hand, {gone:g} contracts written off at the bid {bid:.2f}"
        return f"{row['ticker']}: {gone:g} contracts sold by hand, {real:g} still held"


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
            from . import manualrest

            price = manualrest.rest_fill_price(why) or price  # a resting ask fills at its own price, not the bid this re-read catches
            store.mark_exited(row["ticker"], price, why, lane, seen, side=side)
            return f"paper-sold {row['ticker']} @ {price:.2f}. {why}"
        count = round(store.net_contracts(row["ticker"], s.env), 2)
        real = account_count(client, row["ticker"], side) if count > 0 else None  # what the account really holds: the user may have sold some by hand (2026-11-13: 100 sell orders in 77 s for 1.72 contracts that were gone)
        if real is not None and real < count - 0.05 and book_hand_sale(s, store, row["ticker"], side, real, bid) > 0:
            count = round(store.net_contracts(row["ticker"], s.env), 2)
            if count < 0.005:
                store.mark_exited(row["ticker"], bid, f"closed by hand: the account no longer holds these contracts (booked at the bid {bid:.2f} when the bot noticed, not at your fill). {why}", lane, seen, side=side)
                return f"{row['ticker']}: closed by hand, nothing left to sell. {why}"
        if count <= 0:
            store.mark_exited(row["ticker"], bid, why, lane, seen, side=side)
            return f"paper-sold {row['ticker']} @ {bid:.2f} (no live fill). {why}"
        limit = max(0.01, round(bid - slip, 2))
        profit, floor = is_profit_sale(why), 0.0
        if profit:
            paid = store.entry_fill(row["ticker"], side, float(row["ts"] or 0.0)) or float(row["ask"] or 0.0)
            floor = profit_floor(paid)
            if bid < floor:
                return f"profit take on {row['ticker']} not sent: the bid {bid:.2f} is under the {floor:.2f} the bet cost with both fees. {why}"
            limit = max(limit, floor)  # a profit sale never fills under what the bet cost
        if before_order:
            before_order()
        try:
            sell = client.sell_yes_ioc if side == "yes" else client.sell_no_ioc
            resp = sell(row["ticker"], count, limit)
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
        if left >= 0.005:
            # A sale into a book that has just been cleared fills nothing at a bid read a moment ago (burst and crash exits do this). Read the book again and sell what is left a little lower each time.
            # A profit sale (2026-11-14: a take at 0.88 sold its second leg at 0.860) is retried once, at a fresh read of the bid with no slip and never under its floor; the rest waits for the next quote.
            for extra in PROFIT_RETRY_SLIP if profit else EXIT_RETRY_SLIP:
                time.sleep(0.4)
                try:
                    fresh = best_bid(client.orderbook(row["ticker"]), side)
                except KalshiError:
                    fresh = None
                if fresh is None or fresh < floor:
                    break  # nobody is bidding, or (a profit sale) the bid is under what the bet cost: nothing to sell into
                try:
                    r2 = sell(row["ticker"], round(left, 2), max(0.01, round(fresh - extra, 2), floor))
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
            store.mark_exited(row["ticker"], avg if filled else bid, why, lane, seen, side=side)
            return f"sold {filled:g}/{count} {row['ticker']} @ {avg:.4f}. {why}"
        return f"partial exit {filled:g}/{count} {row['ticker']} @ {avg:.4f}. {why}"


RESOLVE_BATCH = 100  # markets asked for in one request when resolving


def resolve(client: KalshiClient, store: Store, client_for: Callable[[str], KalshiClient] | None = None) -> int:
    """Fill in outcomes for markets that have settled. Returns how many markets resolved."""
    n = 0
    groups: dict[int, tuple[Any, list[str]]] = {}
    for ticker in store.unresolved_tickers():
        book = client_for(ticker) if client_for is not None else client
        groups.setdefault(id(book), (book, []))[1].append(ticker)
    for book, tickers in groups.values():
        found: dict[str, dict[str, Any]] = {}
        if hasattr(book, "markets_by_tickers"):
            for i in range(0, len(tickers), RESOLVE_BATCH):
                try:
                    for m in book.markets_by_tickers(tickers[i:i + RESOLVE_BATCH]):
                        found[m.get("ticker", "")] = m
                except KalshiError:
                    continue  # that batch is read one by one below
        for ticker in tickers:
            m = found.get(ticker)
            if m is None:
                try:
                    m = book.market(ticker)  # not in a batch (a venue without batches, or a market the listing left out)
                except KalshiError:
                    continue
            if m.get("result") in {"yes", "no"}:
                store.set_outcome(ticker, m["result"])
                n += 1
    return n
