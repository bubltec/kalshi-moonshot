"""One scan pass over two lanes, then log every decision.

moonshot: cheap long shots, a small chance of a large payout.
steady:   likely outcomes whose ask has been rising, a small payout that repeats.

For each candidate the model gets the question, similar settled cases, and recent news
headlines. Orders are placed only when MODE=trade and the risk gate agrees.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import NamedTuple

from . import gate, risk, shadow
from .calibration import Calibration
from .commodities import CommodityPricer, load_instruments
from .config import Settings
from .estimators import Estimate, Estimator, build_estimators
from .kalshi import MARKET_PAGES, KalshiClient, KalshiError, KalshiRateLimited
from .lanes import (
    LADDER,
    LaneSpec,
    chance_ok,
    lane_specs,
    main_lanes,
    spec_for,
    takes_profit,
    tiered_lanes,
    wants_bet,
)
from .search import NewsSearch
from .sizing import Sizing, size_bet
from .store import Store
from .strategy import (
    LongShot,
    ask_trend,
    edge,
    falling_reason,
    flip,
    lane_candidates,
    lane_reject_reason,
    one_per_mutex_event,
    order_fee,
    rising,
    side_ask_notional,
    side_prob,
    stop_loss_hit,
    take_profit_hit,
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


def lane_series(spec: LaneSpec, s: Settings) -> list[str]:
    """Series a two-level lane fetches by name: its own list, else every priced commodity series."""
    if spec.series:
        return list(spec.series)
    return [
        i.prefix + suffix for i in load_instruments(s.data_dir) for suffix in i.suffixes
    ]


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
    estimators: dict[str, Estimator] | None, now: datetime | None = None,
) -> list[LongShot]:
    """Level 2, fast: re-read only the watched events and return markets worth evaluating now.

    Markets decided on within the lane's cooldown are dropped, so a two-minute loop does not
    log the same unchanged view thirty times an hour.
    """
    now = now or datetime.now(UTC)
    markets: list[dict] = []
    for event, info in sorted(store.watch(spec.name).items(), key=lambda kv: kv[1].get("close", "")):
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


def _log(store: Store, s: Settings, shot: LongShot, **fields) -> None:
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
) -> Outcome:
    """Rate one market, log the decision, and place an order if allowed.

    The lane's spec names the estimator and the bet rule. The estimate is then weighed against
    our own settled record (calibration.py) before the rule is applied.

    raise_model_errors: let an estimator failure propagate (so a queued job can retry it) instead
    of logging a "model error" pass. order_key makes the order id stable across retries.
    before_order runs just before an order is sent, so the caller can mark that moment.
    """
    spec = spec_for(s, shot.lane)
    estimators = estimators or build_estimators(est, store, searcher, quant)
    try:
        estimator = estimators.get(spec.estimator)
        if estimator is None:
            raise RuntimeError(f"no {spec.estimator!r} estimator available for lane {spec.name}")
        got: Estimate = estimator.estimate(client, s, shot)
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
        open_, gate_note = gate.check(store, spec, shot.close_time)
        if not open_:
            bet = False
            note = f"{gate_note}. {note}"
    if bet and not chance_ok(spec, p):
        bet = False
        note = f"chance {p:.0%} outside the lane's {spec.min_chance:.0%}-{spec.max_chance:.0%}. {note}"
    if bet and shot.mutually_exclusive:
        others = store.bet_tickers_in_event(shot.event_ticker) - {shot.ticker}
        if others:
            bet = False
            note = f"other side already taken ({min(others)}). {note}"
    sized = Sizing(0, 0.0, "")
    if bet:
        balance = store.bankroll.read(client, s, live=s.is_live(shot.lane, shot.series))
        sized = size_bet(
            spec, s, cal.p, shot.ask, balance.available, balance.equity,
            balance.open_by_lane.get(shot.lane, 0.0),
        )
        if store.holds_sized_bet(shot.ticker, shot.lane) and not s.is_live(shot.lane, shot.series):
            sized = Sizing(0, 0.0, "already holding this market (paper)")
        elif not sized.ok:
            bet = False
            note = f"not sized: {sized.why}. {note}"
        else:
            note = f"{sized.why}. {note}"
    if bet and s.is_live(shot.lane, shot.series):
        placed = place(
            s, client, store, shot, note, sized.contracts,
            order_key=order_key, before_order=before_order,
        )
        note = placed.note
        if placed.filled > 0:
            # Record what actually filled, not what was asked for: a partial fill is a smaller bet.
            sized = Sizing(int(placed.filled), placed.cost, sized.why)
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


def _day_start(now: float) -> float:
    from .risk import start_of_day

    return start_of_day(now)


def _top_up(s: Settings, client: KalshiClient, store: Store, shot: LongShot, cost: float) -> str | None:
    """Make sure the exchange this order trades on holds enough cash, moving some in from another exchange if it does not.

    Returns None when the order may go ahead, or the reason it should be skipped. Opt-in (SHARD_TOPUP_TARGET_USD > 0),
    never takes the source below SHARD_TOPUP_KEEP_USD, and never moves more than SHARD_TOPUP_MAX_USD_PER_DAY in a day.
    """
    if s.shard_topup_target <= 0:
        return None
    try:
        idx = _EXCHANGE_OF.get(shot.series)
        if idx is None:
            raw = client.market(shot.ticker).get("exchange_index")
            if raw is None:
                return None
            idx = _EXCHANGE_OF[shot.series] = int(raw)
        cash = client.shard_balances()
        here = cash.get(idx, 0.0)
        if here + 1e-9 >= cost:
            _UNFUNDED.pop(idx, None)
            return None
        want = max(cost - here, s.shard_topup_target - here)
        sources = {i: c - s.shard_topup_keep for i, c in cash.items() if i != idx and c - s.shard_topup_keep > 0}
        if not sources:
            return f"skipped: exchange {idx} holds ${here:.2f}, and no other exchange has cash above the ${s.shard_topup_keep:.0f} floor to move in"
        src = max(sources, key=lambda i: sources[i])
        room = s.shard_topup_max_per_day - store.shard_moved_since(_day_start(time.time()))
        amount = round(min(want, sources[src], room), 2)
        if amount + 1e-9 < cost - here:
            return f"skipped: exchange {idx} needs ${cost - here:.2f} more but the top-up limit allows ${max(amount, 0):.2f} (daily cap ${s.shard_topup_max_per_day:.0f})"
        resp = client.shard_transfer(amount, "event_contract", src, "event_contract", idx)
        store.add_shard_move(src, idx, amount, f"top up for {shot.ticker}", str(resp.get("transfer_id", "")))
        log.info("moved $%.2f from exchange %d to exchange %d for %s", amount, src, idx, shot.ticker)
        for _ in range(8):  # the transfer is asynchronous: wait for it to land
            time.sleep(1.0)
            if client.shard_balances().get(idx, 0.0) + 1e-9 >= cost:
                _UNFUNDED.pop(idx, None)
                return None
        return f"skipped: moved ${amount:.2f} to exchange {idx} but it has not landed yet; the next review retries"
    except KalshiRateLimited:
        raise
    except KalshiError as exc:
        return f"skipped: could not top up exchange cash ({exc})"


class Placed(NamedTuple):
    note: str
    filled: float = 0.0  # contracts that actually filled
    cost: float = 0.0  # dollars spent on them, fee included


def place(
    s: Settings, client: KalshiClient, store: Store, shot: LongShot, why: str, count: int,
    order_key: str | None = None, before_order: Callable[[], None] | None = None,
) -> Placed:
    """Send the order for `count` contracts (sized by sizing.py) if the book and risk gate agree."""
    cost = shot.ask * count + order_fee(shot.ask, count)
    cap = cost  # already sized under every ceiling; the gate re-checks the global ones
    if cost + 1e-9 < s.min_order_usd:
        return Placed(f"skipped: order ${cost:.2f} is under the ${s.min_order_usd:.2f} minimum")
    try:
        offered = side_ask_notional(client.orderbook(shot.ticker), shot.ask, shot.side)
    except KalshiError as exc:
        return Placed(f"skipped: could not read the book ({exc})")
    if offered + 1e-9 < s.min_order_usd:
        return Placed(
            f"skipped: ${offered:.2f} offered at {shot.ask:.2f}, "
            f"under the ${s.min_order_usd:.2f} minimum"
        )
    verdict = risk.check(
        s, store, shot.ticker, cost, max_trade=cap,
        event_ticker=shot.event_ticker, mutually_exclusive=shot.mutually_exclusive,
    )
    if not verdict.ok:
        return Placed(f"blocked by risk: {verdict.reason}")
    topped = _top_up(s, client, store, shot, cost)
    if topped:
        return Placed(topped)
    known = _EXCHANGE_OF.get(shot.series)
    if known is not None and _UNFUNDED.get(known, 0.0) > time.time():
        return Placed(f"skipped: exchange {known} (where {shot.series} trades) has no cash; move funds there in the Kalshi app")
    if before_order:
        before_order()
    try:
        buy = client.buy_yes_ioc if shot.side == "yes" else client.buy_no_ioc
        resp = buy(shot.ticker, count, shot.ask, client_order_id=order_key)
    except KalshiRateLimited:
        raise  # refused outright, so nothing was bought; the queue retries it later
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
        ticker=shot.ticker, env=s.env, count=count, price=shot.ask, fill_count=filled,
        cost_usd=avg * filled + fee, fee_usd=fee, order_id=resp.get("order_id", ""),
        contract=shot.side,
    )
    spent = avg * filled + fee
    return Placed(
        f"bought {shot.side.upper()} {filled:g}/{count} @ {avg:.4f} ({s.env}). {why}", filled, spent
    )


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


def exit_position(
    s: Settings, client: KalshiClient, store: Store, row, bid: float, why: str,
    before_order: Callable[[], None] | None = None,
) -> str:
    """Sell (or paper-sell) one open bet at `bid`. Safe to call from any thread."""
    with _EXIT_LOCK:
        lane = row["lane"] or "moonshot"
        if (row["ticker"], lane) not in {(r["ticker"], r["lane"] or "moonshot") for r in store.open_bets()}:
            return f"{row['ticker']}: already exited"
        side = row["side"] or "yes"
        if not s.is_live(lane, row["ticker"].split("-")[0]):
            store.mark_exited(row["ticker"], bid, why, lane)
            return f"paper-sold {row['ticker']} @ {bid:.2f}. {why}"
        count = int(store.net_contracts(row["ticker"], s.env))
        if count <= 0:
            store.mark_exited(row["ticker"], bid, why, lane)
            return f"paper-sold {row['ticker']} @ {bid:.2f} (no live fill). {why}"
        if before_order:
            before_order()
        try:
            sell = client.sell_yes_ioc if side == "yes" else client.sell_no_ioc
            resp = sell(row["ticker"], count, bid)
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
        if store.net_contracts(row["ticker"], s.env) <= 0:
            store.mark_exited(row["ticker"], avg if filled else bid, why, lane)
            return f"sold {filled:g}/{count} {row['ticker']} @ {avg:.4f}. {why}"
        return f"partial exit {filled:g}/{count} {row['ticker']} @ {avg:.4f}. {why}"


def stop_loss_pass(
    s: Settings,
    client: KalshiClient,
    store: Store,
    *,
    now: datetime | None = None,
    before_order: Callable[[], None] | None = None,
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
        try:
            m = client.market(row["ticker"])
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
        if profit_on and spec is not None:
            why = take_profit_hit(entry, bid, spec.take_profit_x, spec.take_profit_at)
        if not why and stop_on:
            why = stop_loss_hit(entry, bid, hours, s.stop_loss_hours, s.stop_loss_drop)
        if not why:
            continue
        notes.append(exit_position(s, client, store, row, bid, why, before_order))
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


def resolve(client: KalshiClient, store: Store) -> int:
    """Fill in outcomes for markets that have settled. Returns how many markets resolved."""
    n = 0
    for ticker in store.unresolved_tickers():
        try:
            m = client.market(ticker)
        except KalshiError:
            continue
        if m.get("result") in {"yes", "no"}:
            store.set_outcome(ticker, m["result"])
            n += 1
    return n
