"""The `run` loop: pulls due jobs from the queue and runs them one at a time.

scan      fetch the markets in the window, shortlist both lanes, queue one `evaluate` per market,
          and make sure the next scan is scheduled (so a crash never ends the loop)
evaluate  re-read the market's current price, ask the model, log the decision, maybe order
resolve   record the outcome of markets that settled
review    once an hour, tighten a setting when the scorecard makes that clear

Stop it at any time. Finished work stays finished; running work is picked up again on start.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any

from . import jobs as jobs_mod
from . import lanes as lanes_mod
from . import manual as manual_mod
from . import memguard, orderkeys, reqstats, risk, runner, steptime
from . import ticks as ticks_mod
from .commodities import CommodityPricer
from .bankroll import ColdMemory
from .config import Settings
from .estimators import EstimateShare, build_estimators
from .jobs import PLACING, Job, Jobs
from .kalshi import KalshiClient, KalshiError, KalshiRateLimited
from .lanes import POLY_LANES, job_priority, lane_specs, spec_for, tiered, tiered_lanes
from .marketdata import Discovery, shared_for
from .search import NewsSearch
from .store import Store
from .strategy import LongShot, ask_trend, falling_reason, lane_reject_reason, side_view

log = logging.getLogger("moonshot")
# Jobs that are long and not needed to trade (downloading history, resolving settled markets, syncing fills, the broad scan, the hourly review). They run on their own thread,
# so a 40-second job can never hold up a live lane's review or evaluation, and their requests are throttled to a third of the budget.
MAINTENANCE = frozenset({"scan", "resolve", "review", "history", "account_sync", "patterns"})
BUY_KINDS = frozenset({"evaluate"})  # the job that sizes and places a buy. With BUY_WORKERS above 0 these run on their own threads, so a lane scope or review (or another lane's slow buy) never queues a buy behind it
SLOW_JOB_SECONDS = 10.0  # a job that runs longer than this is logged with how long it ran, so a slow stage can be found in the logs
BUY_POLL = 0.5  # seconds a buy worker sleeps when no buy is due
HISTORY_PRIORITY = -10  # runs only when no trading job is waiting (a running job is never interrupted)


def _strike(m: dict) -> float | None:
    for key in ("floor_strike", "cap_strike"):
        try:
            if m.get(key) not in (None, ""):
                return float(m[key])
        except (TypeError, ValueError):
            continue
    return None


def _price(m: dict, key: str) -> float:
    try:
        return float(m.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def knobs(s: Settings) -> tuple:
    """Settings whose change is worth a log line."""
    return (
        s.env, s.mode, tuple(sorted(s.live_lanes or ())), s.fast_live, s.real_money, s.min_order_usd, s.min_offer_ratio, s.max_trade_pct, s.max_loss_pct_per_day, s.min_price, s.max_price, s.min_volume, s.max_spread,
        s.min_hours_to_close, s.min_edge, s.scan_pages, s.shortlist, s.scan_interval_minutes,
        s.model, tuple(sorted(s.skip_categories)), s.skip_numeric, tuple(sorted(s.exclude_versions)),
        s.max_rps, s.page_size, s.job_max_attempts, s.moonshot_max_drop,
        s.steady_enabled, s.steady_min_price, s.steady_max_price, s.steady_min_trend,
        s.steady_max_lag,
        s.review_enabled, tuple(sorted(s.rag_skip_lanes)),
        s.steady_max_hours_to_close, s.steady_min_edge, s.steady_usd_per_trade,
        s.search_enabled, s.search_results,
        s.commodities_enabled, s.commodity_min_edge, s.commodity_min_price, s.commodity_max_price,
        s.commodity_vol_mult, s.commodity_max_basis,
    )


class Worker:
    def __init__(
        self,
        s: Settings,
        client: KalshiClient,
        est,
        store: Store,
        searcher: NewsSearch | None,
        *,
        client_factory: Callable[[Settings], KalshiClient] | None = None,
        est_factory: Callable[[Settings], object] | None = None,
        reload: Callable[[], Settings] | None = None,
        quant: CommodityPricer | None = None,
    ):
        self.s, self.client, self.est, self.store, self.searcher = s, client, est, store, searcher
        self.jobs = Jobs(store)
        self._client_factory, self._est_factory, self._reload = client_factory, est_factory, reload
        self.quant = quant
        self.recorder: Any = None
        self.watcher: Any = None
        self.entry_watcher: Any = None
        self.settlement_edge: Any = None
        self.discovery: Any = None
        self._hot_local = threading.local()  # the discovery thread's own store connection
        self._tape_db: Any = None  # a read connection to the tape, opened on first use, for the entry factors saved with decisions
        self.stopping = False
        self._mem_ready = threading.Event()  # set when _warm_memory has filled what a socket buy reads
        self._warm_store: Store | None = None
        self._poly_client: Any = None  # PolymarketClient, False once building it has failed, None until first use
        self._poly_stream: Any = None
        self._last_cleanup = 0.0
        self.last_progress = time.time()  # when a job last finished, for the watchdog heartbeat (heartbeat.py)
        self.share = EstimateShare()  # one estimate per market, reused by every lane that asks soon after
        self._split = False  # True once run_forever has started the maintenance thread: the main loop then leaves those jobs to it
        self._lane_threads: set[str] = set()  # the live lanes whose scope and review jobs run on a thread of their own (run_forever)
        self._buy_threads = 0  # how many dedicated buy workers run_forever started: while it is above 0 the main loop leaves evaluate jobs to them

    # ---- lifecycle ----
    def start(self) -> None:
        """Recover from the last run, and scan right away (a restart is how you force a re-run)."""
        from .runner import fund_open_exchanges

        try:
            fund_open_exchanges(self.s, self.client, self.store)
        except Exception:
            log.exception("could not fund exchanges before trading; continuing")
        resumed, parked = self.jobs.recover()
        if resumed:
            log.info("resuming %d job(s) that were running when the bot stopped", resumed)
        if parked:
            log.warning(
                "%d job(s) were mid-order when the bot stopped and were NOT re-run. "
                "Check your Kalshi portfolio, then see `jobs`.", parked,
            )
        self.jobs.run_now("scan")
        self.jobs.ensure("review", time.time(), max_attempts=self.s.job_max_attempts)
        if self.s.account_sync_enabled and getattr(self.client, "authed", False):
            self.jobs.ensure(
                "account_sync", time.time() + 90, max_attempts=self.s.job_max_attempts, priority=HISTORY_PRIORITY
            )
        if self.s.patterns_hours > 0 and self.s.account_sync_enabled:
            self.jobs.ensure(
                "patterns", time.time() + 600, max_attempts=self.s.job_max_attempts, priority=HISTORY_PRIORITY
            )
        if self.s.history_enabled:
            self.jobs.ensure(
                "history", time.time() + 300, max_attempts=self.s.job_max_attempts, priority=HISTORY_PRIORITY
            )
        self._ensure_tiered()
        self._start_discovery()
        self._start_tape()
        threading.Thread(target=self._quote_polymarket, name="polymarket-paper", daemon=True).start()
        from . import forecast_fit

        forecast_fit.start_refit(lambda: self.s)  # idle unless a forecast lane is on
        from . import store as store_mod

        store_mod.queue_decisions()  # a pass's decision row is written by a background thread: an evaluation does not end on an INSERT and a COMMIT
        Store(self.s.db_path, self.s.exclude_versions).rebuild_book()  # the bet book is read once here, before anything is evaluated; from then on whoever writes a bet, a sale or a settlement rebuilds it
        store_mod.serve_from_memory()  # remembered reads (open bets and the like) are re-read by a background thread once their time is up, not by an evaluation
        from . import calibration

        calibration.start_background(lambda: Store(self.s.db_path, self.s.exclude_versions))  # the record adjustment is answered from memory from here on: no evaluation reads the settled decisions
        from . import riskrecord

        riskrecord.start_background(lambda: self.s, lambda: Store(self.s.db_path, self.s.exclude_versions))  # every open market's risk attributes every 30 s, from its own thread
        from . import backup

        backup.start_nightly(lambda: self.s)
        from . import archive
        from .tape import Tape

        archive.start_nightly(
            lambda: self.s, lambda: Store(self.s.db_path, self.s.exclude_versions), lambda: Tape(self.s.tape_path).db
        )  # idle until ARCHIVE_BUCKET is set
        from . import sitepub

        sitepub.start(lambda: self.s, lambda: Store(self.s.db_path, self.s.exclude_versions))  # idle until SITE_BUCKET is set
        from . import control

        def publish_admin(s: Settings) -> None:
            sitepub.publish_admin_once(s, Store(s.db_path, s.exclude_versions), sitepub.client_for(s))

        control.start(lambda: self.s, publish_admin)  # the admin page's lane changes; idle until SITE_BUCKET is set
        from . import heartbeat

        def progress() -> tuple[float, float]:
            rec = self.recorder
            beat = max((getattr(x, "last_event_at", None) or 0.0) for x in (getattr(rec, "stream", None), getattr(rec, "extra_stream", None)))
            return self.last_progress, beat

        heartbeat.start(lambda: self.s.heartbeat_enabled, self.s.heartbeat_region, progress)  # the watchdog's heartbeat; idle unless HEARTBEAT_ENABLED

    def _start_tape(self) -> None:
        """Record quotes next to the feed price they follow (read-only; see tape.py)."""
        if not self.s.tape_enabled or not getattr(self.client, "authed", False) or self.recorder is not None:
            return
        from .tape import TapeRecorder

        try:
            self.recorder = TapeRecorder(self.client, self.s, getattr(self.quant, "feed", None))
            self.recorder.start()
            log.info("tape recorder on: %s (kept %.0f days)", self.s.tape_path, self.s.tape_keep_days)
            from . import restbuys

            self.recorder.listeners.append(restbuys.on_event)  # a streamed fill or quote wakes the resting buy waiting on it
            self._start_watcher()
            self._start_entry_watcher()
            self._start_market_events()
            self._start_settlement_edge()
        except Exception:
            log.exception("could not start the tape recorder; continuing without it")
            self.recorder = None

    def _start_watcher(self) -> None:
        """Sell on the live socket when a lane's take-profit level is hit (only if some lane asks for one)."""
        from . import watcher

        if self.recorder is None or self.watcher is not None or not watcher.wanted(self.s):
            return
        db_path = self.s.db_path
        w = watcher.ProfitWatcher(
            lambda: self.s, self.client, lambda: Store(db_path, self.s.exclude_versions), hold_value=self._hold_value, spot_side=self._spot_side
        )
        self.recorder.listeners.append(w.on_event)
        w.follow = self.recorder.follow
        w.tape_factory = self._open_tape
        self.recorder.extra_tickers = lambda: self._held_for_watching() | w.manual_tickers()  # hourly, daily and hand-placed positions are streamed too
        w.client_for = self._venue  # a Polymarket stop sells on Polymarket; every other lane keeps the Kalshi client
        w.start()
        self.watcher = w
        log.info("take-profit watcher on: selling from the live socket when a lane's level is hit")

    def _start_entry_watcher(self) -> None:
        """Review a watched event the moment a streamed quote or its settlement index ticks (entrywatch.py; user, 2026-11-19: "entry is supposed to be on a thread based on the websocket")."""
        from . import entrywatch

        if self.recorder is None or self.entry_watcher is not None or not entrywatch.wanted(self.s):
            return
        db_path = self.s.db_path
        w = entrywatch.EntryWatcher(
            lambda: self.s, self.client, lambda shared=False: Store(db_path, self.s.exclude_versions, shared=shared),
            lambda store: build_estimators(self.est, store, self.searcher, self.quant),
        )
        self.recorder.listeners.append(w.on_event)
        w.client_for = self._venue  # the same review, on the lane's own venue
        w.evaluate = self.evaluate_shot  # each hit is evaluated on its market's own thread, in memory: no job row (orderkeys.py has the order id)
        threading.Thread(target=self._seed_order_keys, name="order-keys-seed", daemon=True).start()
        threading.Thread(target=self._warm_memory, name="memory-fill", daemon=True).start()
        w.start()
        self.entry_watcher = w
        log.info("entry watcher on: reviewing a watched event as soon as its socket quote or index ticks into the lane's price band")

    def _warm_memory(self) -> None:
        """A background job (2026-11-19: database and Kalshi reads belong to background jobs only): fills what a buy decision reads from memory (the earlier decisions of each market for the cooldown, the open positions, the account balance, today's realized result) and keeps it fresh every Bankroll.REFRESH_EVERY seconds. The socket path decides nothing until the first pass is done."""
        from . import store as store_mod

        first = True
        while not self.stopping:
            try:
                with memguard.allowed():
                    store = Store(self.s.db_path, self.s.exclude_versions) if first else self._warm_store
                    self._warm_store = store
                    if first:
                        store.preload_decisions()
                    store.open_position_tickers(self.s.env)
                    clients = {id(c): c for c in (self._venue(name) for name in lane_specs(self.s))} or {id(self.client): self.client}
                    for c in clients.values():
                        self.store.bankroll.refresh(c, self.s)
                    risk.refresh_realized(self.store)
                if first:
                    self.store.bankroll.served = True
                    risk.serve(self.store)
                    self._mem_ready.set()
                    first = False
                    log.info("memory filled: the buy decision reads the cooldown, bet book, positions, balance and daily result from memory")
            except Exception:
                log.exception("memory fill failed; the socket path buys nothing until it has run once" if first else "memory refresh failed; the last copy stays in use")
            time.sleep(self.store.bankroll.REFRESH_EVERY)

    def _seed_order_keys(self) -> None:
        """A background job: the orders already sent, so a buy's order id after a restart is never one Kalshi has seen (orderkeys.py)."""
        while not self.stopping:
            try:
                counts = Store(self.s.db_path, self.s.exclude_versions).order_counts(self.s.env, time.time() - 3 * 86400)
                orderkeys.seed(self.s.env, counts)
                return
            except Exception:
                log.exception("order keys: could not read the orders table; no socket buy until it can be read")
                time.sleep(5.0)

    def _start_settlement_edge(self) -> None:
        """Log, as paper bets, the side whose exact settlement chance beats its ask in the last 150 s of every crypto 15-minute market (settlement_edge.py). Measure only; its own thread."""
        from . import settlement_edge

        if self.recorder is None or self.settlement_edge is not None or not self.s.settlement_edge_enabled:
            return
        db_path = self.s.db_path
        w = settlement_edge.SettlementEdge(lambda: self.s, self.client, lambda: Store(db_path, self.s.exclude_versions), self._open_tape)
        self.recorder.listeners.append(w.on_event)
        w.start()
        self.settlement_edge = w
        log.info("settlement edge on: logging the cheap side whose exact settlement chance beats its ask (paper only)")

    def _spot_side(self, ticker: str, side: str) -> float | None:
        """The chance, from the live spot price against the strike alone (no calibration), that this side pays. None when the market cannot be priced that way."""
        from .strategy import LongShot, side_prob

        if self.quant is None:
            return None
        series = ticker.split("-")[0]
        shot = LongShot(
            ticker=ticker, title="", rules="", bid=0.0, ask=0.0, volume=0.0, close_time="", hours_to_close=0.1,
            series=series, event_ticker=ticker.rsplit("-", 1)[0], side=side,
        )
        p_yes, _ = self.quant.estimate(self.client, shot, self.s)
        return side_prob(p_yes, side)

    def _hold_value(self, row, store: Store) -> float | None:
        """What the model says a position is worth if held to settlement: its calibrated chance of paying (ladder lanes only)."""
        from datetime import UTC, datetime

        from . import lanes
        from .strategy import LongShot, side_prob

        spec = lanes.lane_specs(self.s).get(row["lane"] or "moonshot")
        if spec is None or spec.estimator != lanes.LADDER or self.quant is None:
            return None
        side = row["side"] or "yes"
        try:
            hours = max(0.0, (datetime.fromisoformat(row["close_time"]) - datetime.now(UTC)).total_seconds() / 3600.0)
        except (TypeError, ValueError):
            return None
        ask = float(row["ask"] or 0.0)
        shot = LongShot(
            ticker=row["ticker"], title="", rules="", bid=0.0, ask=ask, volume=0.0, close_time=row["close_time"],
            hours_to_close=hours, series=row["ticker"].split("-")[0], event_ticker=row["event_ticker"] or "", side=side,
        )
        p_yes, _ = self.quant.estimate(self.client, shot, self.s)
        return store.calibrator.adjust(spec.name, row["category"] or "", ask, side_prob(p_yes, side), spec.trust).p

    def _held_for_watching(self) -> set[str]:
        """Tickers of open bets whose lane has an exit module: the socket should stream them."""
        from . import lanes

        specs = lanes.lane_specs(self.s)
        out = set()
        for row in self.store.open_bets():
            if (row["lane"] or "") in POLY_LANES:
                continue  # Polymarket bets are quoted by the Polymarket socket, not Kalshi's
            spec = specs.get(row["lane"] or "moonshot")
            if spec is not None and spec.sell_chain:
                out.add(row["ticker"])
        return out

    def _watch_new_position(self, ticker: str) -> None:
        """A bet was just placed: follow it on the socket now and register it with the watcher, instead of at the next refresh."""
        if self.watcher is None or self.recorder is None:
            return
        try:
            self.recorder.follow({ticker})
            self.watcher.refresh()
        except Exception:
            log.exception("could not register %s with the watcher", ticker)

    def _start_discovery(self) -> None:
        """One thread lists the open markets of every series any lane uses and fills the shared cache (marketdata.py); lane jobs read from it, so a lane costs no requests."""
        if self.discovery is not None or not hasattr(self.client, "cache"):
            return
        self.discovery = Discovery(
            lambda: self.client, lambda: self.s,
            lambda s: {name for spec in tiered_lanes(s) for name in runner.lane_series(spec, s)},
            hot=self._hot_series,
        )
        self.discovery.start()

    def _hot_series(self, s: Settings) -> list[str]:
        """The series to list quickly, most important first: those with an open bet, then those with a prospect on a lane's watchlist (the live lanes' before the paper lanes').
        Runs on the discovery thread, so it reads through a connection of its own."""
        store = getattr(self._hot_local, "store", None)
        if store is None:
            store = self._hot_local.store = Store(s.db_path, s.exclude_versions)
        live = s.live_lanes or frozenset()
        out = [row["ticker"].split("-")[0] for row in store.open_bets()]
        for spec in sorted(tiered_lanes(s), key=lambda x: x.name not in live):
            out += [info.get("series") or event.split("-")[0] for event, info in store.watch(spec.name).items()]
        return list(dict.fromkeys(out))

    def _shared(self, max_age: float) -> Any:
        """Reads of listings and markets inside this block may come from the shared cache (a no-op when discovery is off or the client has no cache)."""
        return shared_for(self.client, max_age, self.s.discovery_enabled)

    def stop(self) -> None:
        self.stopping = True
        from . import store as store_mod

        store_mod.flush_decisions(3.0)  # the pass rows still queued are written before the process goes
        if self.discovery is not None:
            self.discovery.stop()
        if self.watcher is not None:
            self.watcher.stop()
        if self.recorder is not None:
            self.recorder.stop()

    def run_forever(self) -> None:
        self.start()
        log.info("edits to .env apply on the next job; no rebuild. Stop with ctrl-c or `docker compose stop`.")
        self._split = True
        threading.Thread(target=self._maintenance_loop, name="maintenance", daemon=True).start()
        threading.Thread(target=reqstats.log_loop, args=(lambda: self.stopping,), name="req-stats", daemon=True).start()
        for i in range(self.s.buy_workers):
            threading.Thread(target=self._buy_loop, name=f"buy-job-{i}", daemon=True).start()
        self._buy_threads = self.s.buy_workers
        if self._buy_threads:
            log.info("%d buy worker(s) run evaluate jobs apart from scans and reviews", self._buy_threads)
        if self.s.live_lane_threads and self.s.live_lanes is not None:
            for spec in tiered_lanes(self.s):
                if self.s.is_live(spec.name):
                    self._lane_threads.add(spec.name)
                    threading.Thread(target=self._lane_loop, args=(spec.name,), name=f"live-lane-{spec.name}", daemon=True).start()
            if self._lane_threads:
                log.info("live lane thread(s) run the scope and review of %s apart from every other job", ", ".join(sorted(self._lane_threads)))
        while not self.stopping:
            if not self.step():
                time.sleep(1.0)
        log.info("stopped cleanly")

    def _maintenance_once(self) -> bool:
        """Claim and run one due maintenance job, throttled. Returns False when none was due."""
        job = self.jobs.claim(kinds=MAINTENANCE)
        if job is None:
            return False
        with self.client.background() if hasattr(self.client, "background") else contextlib.nullcontext():
            self.run_job(job)
        return True

    def _maintenance_loop(self) -> None:
        while not self.stopping:
            try:
                busy = self._maintenance_once()
            except Exception:
                log.exception("maintenance loop")
                busy = False
            if not busy:
                time.sleep(1.0)

    def _buy_loop(self) -> None:
        """A buy worker: claims only evaluate jobs, so what holds up a buy is another buy, never a scan, review or scope."""
        while not self.stopping:
            jobs_mod.WAKE.clear()  # cleared before the claim: a job enqueued after this sets it again, so the wait below returns at once (no lost wake-up)
            try:
                job = self.jobs.claim(kinds=BUY_KINDS)
                if job is not None:
                    self.run_job(job)
                    continue
            except Exception:
                log.exception("buy worker")
            jobs_mod.WAKE.wait(BUY_POLL)  # an enqueue wakes it in microseconds; BUY_POLL is only the fallback for a job another process queued

    def _lane_loop(self, name: str) -> None:
        """A live lane's own thread: claims only that lane's scope and review jobs, so what holds up its watch list is its own previous pass, never a paper lane's or a maintenance job (2026-11-12: live scopes waited over 10 s in about 6% of passes behind jobs that ran 24 to 900 s)."""
        kinds = frozenset({f"lane_scope:{name}", f"lane_review:{name}"})
        while not self.stopping:
            try:
                job = self.jobs.claim(kinds=kinds)
                if job is not None:
                    self.run_job(job)
                    continue
            except Exception:
                log.exception("live lane thread %s", name)
            time.sleep(BUY_POLL)

    def lane_kinds(self) -> frozenset[str]:
        """The job kinds the live lane threads own, left out of the main loop."""
        return frozenset(f"{p}:{n}" for n in self._lane_threads for p in ("lane_scope", "lane_review"))

    def step(self) -> bool:
        """Claim and run one due job. Returns False when nothing was due."""
        self.refresh()
        self._housekeeping()
        left_out = (MAINTENANCE | (BUY_KINDS if self._buy_threads else frozenset()) | self.lane_kinds()) if self._split else None
        job = self.jobs.claim(exclude=left_out)
        if job is None:
            return False
        self.run_job(job)
        return True

    def _housekeeping(self) -> None:
        if time.time() - self._last_cleanup > 3600:
            self._last_cleanup = time.time()
            self.jobs.cleanup()
            self.store.prune_screened()

    # ---- settings hot-reload ----
    def refresh(self) -> None:
        if self._reload is None:
            return
        try:
            fresh = self._reload()
        except Exception:
            log.exception("could not reload settings; keeping the previous ones")
            return
        s = self.s
        if self._client_factory and (fresh.base_url != s.base_url or fresh.key_id != s.key_id or fresh.private_key_path != s.private_key_path):
            self.client = self._client_factory(fresh)  # the old client is left open: the watcher and the other threads still hold it, and a closed client fails every request they send
        elif (fresh.max_rps != s.max_rps or fresh.page_size != s.page_size) and hasattr(self.client, "set_rate"):
            self.client.set_rate(fresh.max_rps, fresh.page_size)  # the same client, so every thread gets the new rate
        if self._est_factory and (fresh.ollama_url != s.ollama_url or fresh.model != s.model):
            self.est._http.close()
            self.est = self._est_factory(fresh)
        if fresh.data_dir != s.data_dir:
            self.store = Store(fresh.db_path, fresh.exclude_versions)
            self.jobs = Jobs(self.store)
        if fresh.exclude_versions != self.store.exclude_versions:
            self.store.exclude_versions = fresh.exclude_versions
            self.store.rebuild_rag()
        if knobs(fresh) != knobs(s):
            log.info(
                "settings reloaded: env=%s mode=%s max_price=%s min_edge=%s interval=%sm",
                fresh.env, fresh.mode, fresh.max_price, fresh.min_edge, fresh.scan_interval_minutes,
            )
        self.s = fresh
        if self.recorder is not None:
            self.recorder.s = fresh  # the tape follows WATCH_SERIES and TAPE_EXTRA_SERIES as they change, without a restart
        if self.searcher:
            self.searcher.results = fresh.search_results

    # ---- running a job ----
    def run_job(self, job: Job) -> None:
        handlers = {
            "scan": self.do_scan, "evaluate": self.do_evaluate,
            "resolve": self.do_resolve,
            "review": self.do_review, "history": self.do_history, "account_sync": self.do_account_sync, "patterns": self.do_patterns,
        }
        steptime.begin()
        reqstats.set_source(job.kind)
        try:
            handler = handlers.get(job.kind)
            if handler is None and job.kind.startswith(("lane_scope:", "lane_review:")):
                handler = self.do_lane_scope if job.kind.startswith("lane_scope:") else self.do_lane_review
            if handler is None:
                raise ValueError(f"unknown job kind {job.kind!r}")
            began = time.time()
            self.jobs.complete(job.id, handler(job) or "")
            self.last_progress = time.time()
            if job.kind == "evaluate":
                steptime.tick_record("buy evaluate", (time.time() - began) * 1000.0)
                shot = job.payload.get("shot") or {}
                log.info("buy timing %s %s: %.0f ms from the queue to the start of the evaluation, %.1f ms evaluating and deciding. %s", shot.get("lane", ""), shot.get("ticker", ""),
                         max(0.0, began - job.created_ts) * 1000.0, (time.time() - began) * 1000.0, steptime.breakdown_ms())
            if time.time() - began > SLOW_JOB_SECONDS:
                shot = job.payload.get("shot") or {}
                log.info("slow job %d (%s%s): ran %.0fs, %.0fs after it was queued. %s", job.id, job.kind, f" {shot.get('lane', '')} {shot.get('ticker', '')}" if shot else "", time.time() - began, began - job.created_ts, steptime.summary())
        except KalshiRateLimited as exc:
            log.warning("job %d (%s) rate limited; trying again in %.0fs", job.id, job.kind, exc.retry_after)
            self.jobs.defer(job, exc.retry_after, str(exc))
        except Exception as exc:
            verdict = self.jobs.fail(job, f"{type(exc).__name__}: {exc}")
            if verdict == "dead":
                log.error("job %d (%s) gave up after %d tries: %s", job.id, job.kind, job.attempts, exc)
            else:
                log.warning("job %d (%s) failed (try %d/%d), will retry: %s",
                            job.id, job.kind, job.attempts, job.max_attempts, exc)
        finally:
            steptime.end()
            reqstats.set_source(None)

    def do_scan(self, job: Job) -> str:
        s = self.s
        every = s.scan_interval_minutes * 60
        # Scheduled first, so even a scan that crashes or is killed leaves the next one waiting.
        self.jobs.ensure("scan", time.time() + every, max_attempts=s.job_max_attempts)
        # The all-markets scan reads every open market closing in the next 2 to 47 hours (9,273 of them on 2026-11-14: about 220 requests, a burst of 429s at every start and every half hour) to shortlist for the lanes it serves. When no enabled lane is served by it (every running lane is two-level and event-driven) there is nothing to shortlist, and the read is skipped.
        markets = runner.candidate_markets(s, self.client, partial_ok=job.last_attempt) if lanes_mod.main_lanes(s) else []
        lanes = runner.shortlists(s, self.client, markets, self.estimators())
        shots = [shot for picked in lanes.values() for shot in picked]
        log.info(runner.describe_scan(
            s, len(markets), {k: len(v) for k, v in lanes.items()}, self.searcher,
        ))
        queued = 0
        for shot in shots:
            made = self.jobs.enqueue(
                "evaluate", {"shot": asdict(shot)}, dedupe_key=f"evaluate:{shot.lane}:{shot.ticker}",
                expires_in=every, max_attempts=s.job_max_attempts,
                priority=job_priority(s, spec_for(s, shot.lane)),
            )
            queued += 1 if made else 0
        self.jobs.enqueue("resolve", dedupe_key="resolve", max_attempts=s.job_max_attempts)
        self._ensure_tiered()
        return f"{len(markets)} markets, queued {queued} of {len(shots)} evaluations"

    def _poly(self) -> Any:
        """The Polymarket client, or None when it cannot be built. Public market data needs no key."""
        if self._poly_client is False:
            return None
        if self._poly_client is None:
            from .polymarket import client_from_env

            try:
                self._poly_client = client_from_env()
            except Exception as exc:
                log.warning("polymarket paper lane off: %s", exc)
                self._poly_client = False
                return None
        return self._poly_client

    def _venue(self, lane: str) -> Any:
        """Where a lane's markets are read. A Polymarket lane never falls back to Kalshi."""
        if lane in POLY_LANES:
            client = self._poly()
            if client is None:
                raise KalshiError("polymarket paper lane has no client")
            return client
        return self.client

    def _on_poly_quote(self, event: dict) -> None:
        """A Polymarket price, handed to the same entry and exit listeners the Kalshi socket uses."""
        if self.watcher is not None:
            self.watcher.on_event(event)

    def _quote_polymarket(self) -> None:
        """Keep the Polymarket price socket on watched markets and open bets. Read HTTP only while that socket is down."""
        while not self.stopping:
            time.sleep(2.0)
            client = self._poly()
            if client is None:
                continue
            if self._poly_stream is None:
                from .polyws import PolymarketStream

                self._poly_stream = PolymarketStream(client, self._on_poly_quote)
                self._poly_stream.start()
            try:
                store = Store(self.s.db_path, self.s.exclude_versions)
                tickers = {r["ticker"] for r in store.open_bets() if (r["lane"] or "") in POLY_LANES}
                for name in POLY_LANES:
                    tickers.update(store.watch(name))
            except Exception:
                log.debug("polymarket quotes: could not read open bets", exc_info=True)
                continue
            self._poly_stream.set_tickers(tickers)
            if self._poly_stream.connected:
                continue
            for ticker in tickers:
                try:
                    m = client.market(ticker)
                    bid, ask = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 0)
                except Exception:
                    log.debug("polymarket quote failed for %s", ticker, exc_info=True)
                    continue
                if ask <= 0:
                    continue
                self._on_poly_quote({"kind": "quote", "ts": time.time(), "ticker": ticker, "bid": bid, "ask": ask, "src": "poly"})

    def do_evaluate(self, job: Job) -> str:
        """A live lane's evaluation always reads the real market (it is about to place an order); a paper lane's may use the shared cache."""
        lane = job.payload["shot"].get("lane", "")
        live = self.s.is_live(lane)
        ticker = str(job.payload["shot"].get("ticker", ""))
        if live and ticker:
            manual_mod.buying_begin(ticker)  # the fill reaches the account before the order reaches the books: until then the position must not be read as hand-placed
        try:
            with shared_for(self._venue(lane), 0.0 if live else self.s.discovery_eval_age, self.s.discovery_enabled):
                return self._evaluate(job)
        finally:
            if live and ticker:
                manual_mod.buying_end(ticker)

    def _evaluate(self, job: Job) -> str:
        """The database-queue path (a lane on timers while the feed is down, and the CLI): the order id comes from the job, so a rerun after a crash cannot buy twice."""
        order_key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"moonshot/{self.s.env}/{job.created_ts}/{job.id}"))
        return self._evaluate_shot(
            LongShot(**job.payload["shot"]), order_key, raise_model_errors=not job.last_attempt,
            before_order=lambda: self.jobs.checkpoint(job.id, PLACING),
        )

    def evaluate_shot(self, shot: LongShot) -> str:
        """The socket path (entrywatch.py): the buy module's evaluation of one shot on its market's own thread, no job row. A live lane reads the real market; a paper lane may use the shared cache. The order id is derived before the order is sent and is the same after a restart (orderkeys.py); the decision and order rows are written behind."""
        s = self.s
        if not orderkeys.ready() or not self._mem_ready.is_set():
            return f"skip {shot.ticker}: memory is still being filled from the database (order ids, bet book, balance); no buy is decided on a cold copy"
        live = s.is_live(shot.lane)
        if live and shot.ticker:
            manual_mod.buying_begin(shot.ticker)  # the fill reaches the account before the order reaches the books: until then the position must not be read as hand-placed
        try:
            with memguard.decision("buy"), shared_for(self._venue(shot.lane), 0.0 if live else s.discovery_eval_age, s.discovery_enabled):
                return self._evaluate_shot(
                    shot, orderkeys.key(s.env, shot.lane, shot.ticker, shot.side), raise_model_errors=False,
                    before_order=lambda: orderkeys.begin(s.env, shot.ticker, shot.side), memory=True,
                )
        except ColdMemory as exc:
            log.info("skip %s %s: %s", shot.lane, shot.ticker, exc)
            return f"skipped: {exc}"
        finally:
            if live and shot.ticker:
                manual_mod.buying_end(shot.ticker)

    def _evaluate_shot(self, shot: LongShot, order_key: str, raise_model_errors: bool, before_order: Callable[[], None], memory: bool = False) -> str:
        s = self.s
        if ticks_mod.reload_pending():
            return f"skip {shot.ticker}: memory is still reloading from the tape after the restart (no buy is decided on empty history)"
        client = self._venue(shot.lane)
        # The shortlist may be minutes old. Re-read the market and drop it if it no longer fits.
        with steptime.step("market read"):
            if memory and hasattr(client, "market_memory"):  # the socket path: the stream's copy of the market, never a request (memguard.py)
                m = client.market_memory(shot.ticker)
                if m is None:
                    return f"skipped: the stream has no quote for {shot.ticker} in memory yet"
            else:
                m = client.market(shot.ticker)
        m["category"] = shot.category
        if shot.series:
            m.setdefault("series_ticker", shot.series)
        now = datetime.now(UTC)
        spec = spec_for(s, shot.lane)
        m.setdefault("event_ticker", shot.event_ticker)
        why_not = lane_reject_reason(side_view(m, shot.side), spec, s, now)
        if not why_not and spec.trend == "not_falling" and not memory:  # (the socket's review reads no candles: a lane with a trend rule is judged on the quote alone there)
            try:
                candles = client.candlesticks(shot.series, shot.ticker, spec.lookback_hours)
            except Exception:
                candles = []
            why_not = falling_reason(ask_trend(candles, shot.side), spec.max_drop)
        if not why_not and shot.mutually_exclusive:
            others = self.store.bet_tickers_in_event(shot.event_ticker) - {shot.ticker}
            if others:
                why_not = f"other side already taken ({min(others)})"
        if why_not:
            log.info("skip %s: no longer qualifies (%s)", shot.ticker, why_not)
            return f"skipped: {why_not}"
        view = side_view(m, shot.side)
        from . import store as store_mod

        if store_mod.writes_in_background():  # the running bot: the entry snapshot (four or five reads of the tape) is a record, not part of the judgement, so it is taken when the row is written, as of the decision's time
            strike_now = _strike(m)
            shot = replace(shot, bid=_price(view, "yes_bid_dollars"), ask=_price(view, "yes_ask_dollars"), factors="",
                           factors_job=lambda ts, shot=shot, spec=spec, strike_now=strike_now: self._entry_factors(shot, spec, strike_now, ts))
        else:
            with steptime.step("entry factors"):
                factors_text = self._entry_factors(shot, spec, _strike(m))
            shot = replace(shot, bid=_price(view, "yes_bid_dollars"), ask=_price(view, "yes_ask_dollars"), factors=factors_text)
        with runner.lane_lock(shot.lane):  # evaluations of one lane run one after another, so its checks (lane full, cooling down, other side taken) see each other's bets
            out = runner.evaluate(
                s, client, self.est, self.store, shot, self.searcher,
                quant=self.quant, estimators=self.estimators(), share=self.share,
                raise_model_errors=raise_model_errors,
                order_key=order_key,
                before_order=before_order,
            )
        print(runner.format_outcome(out), flush=True)
        if out.action == "bet":
            with memguard.allowed():  # arming the sell watcher for the new position reads the orders table; it comes after the order, not in the judgement
                self._watch_new_position(out.ticker)
        return f"{out.action} {out.ticker}"

    def _open_tape(self) -> Any:
        """A read connection to the tape. Each thread that reads it opens its own."""
        from . import db

        return db.reader(self.s.tape_path)

    def _entry_factors(self, shot: LongShot, spec, strike: float | None = None, now: float | None = None) -> str:
        """Every feature we know of at the moment of the buy, saved with the decision so factors, the forecaster and the model can be read against results later. Any market with a tape quote;
        never blocks a decision."""
        if not self.s.tape_enabled:
            return ""
        from . import factors

        return factors.snapshot_now(self.s, shot.ticker, shot.side, shot.close_time, "", strike, now)

    # ---- two-level lanes: a slow scoping pass feeds a fast reviewing pass ----
    def _ensure_tiered(self) -> None:
        """Keep one scope and one review job alive per two-level lane (also picks up a lane
        turned on by a settings change)."""
        for spec in tiered_lanes(self.s):
            if self._driven(spec):
                continue  # kept by the stream's events (see _on_market_event). The skip holds only while the entry watcher's threads are alive and the feed is up: a dead thread makes the lane undriven and this puts its timers back
            now = time.time()
            self.jobs.ensure(
                f"lane_scope:{spec.name}", now, max_attempts=self.s.job_max_attempts,
                priority=job_priority(self.s, spec),
            )
            self.jobs.ensure(
                f"lane_review:{spec.name}", now + 15, max_attempts=self.s.job_max_attempts,
                priority=job_priority(self.s, spec),
            )

    def _driven(self, spec) -> bool:
        """True when this lane's review runs on the stream (the entry watcher: a socket quote or index tick) and its scope on the stream's market events, with no timer behind them. Only while the feed is up and the entry watcher's threads are alive: otherwise the timers run as before. 2026-11-19 OUTAGE: a lane marked driven with no thread behind it had no timer and nothing to wake it; no lane reviewed for an hour. Now the lane counts as driven only while the entry watcher's threads are alive (EntryWatcher.alive), and _ensure_tiered puts the timers back the moment they are not."""
        cache = getattr(self.client, "cache", None)
        w = self.entry_watcher
        return bool(tiered(spec) and w is not None and w.alive() and cache is not None and hasattr(cache, "fed") and cache.fed())

    def _start_market_events(self) -> None:
        """A thread that turns the stream's market events into scope runs: a new or reopened market, or a changed close, scopes the lanes that trade its series at once; the stream coming up scopes every lane, and the stream going down puts the timers back."""
        if getattr(self, "_market_q", None) is not None or self.recorder is None:
            return
        self._market_q: queue.SimpleQueue = queue.SimpleQueue()
        self.recorder.market_listeners.append(lambda e: self._market_q.put(("market", e)))
        cache = getattr(self.client, "cache", None)
        if cache is not None and hasattr(cache, "fed_listeners"):
            cache.fed_listeners.append(lambda up: self._market_q.put(("feed", up)))
        threading.Thread(target=self._run_market_events, name="market-events", daemon=True).start()

    def _run_market_events(self) -> None:
        store = Store(self.s.db_path, self.s.exclude_versions)
        jobs = Jobs(store)
        while not self.stopping:
            try:
                kind, item = self._market_q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                if kind == "market":
                    self._note_window(store, item)
                self._on_market_event(jobs, kind, item)
            except Exception:
                log.exception("market event %s failed", kind)

    @staticmethod
    def _note_window(store: Store, e: dict[str, Any]) -> None:
        """A 15-minute window's strike and result, kept from the stream's events (Store.note_window) so the window offset is learned with no REST read."""
        from .marketdata import MarketCache

        ticker = str(e.get("ticker") or "")
        if e.get("event") not in {"strike", "determined"} or not ticker.split("-")[0].endswith("15M"):
            return
        close = MarketCache._close_from_ticker(ticker)
        if not close:
            return
        ts = int(datetime.fromisoformat(close).timestamp())
        if e.get("event") == "strike":
            store.note_window(ticker, ts, strike=float(e["strike"]) if e.get("strike") else None)
        else:
            store.note_window(ticker, ts, result=str(e.get("result") or "").lower() or None)

    def _on_market_event(self, jobs: Jobs, kind: str, item: Any) -> None:
        """One market event or feed change onto the scope jobs. Separate from the thread so a test can drive it."""
        if kind == "feed":
            if item:
                for spec in tiered_lanes(self.s):
                    if self._driven(spec):
                        jobs.run_now(f"lane_scope:{spec.name}", priority=job_priority(self.s, spec))
            else:
                for spec in tiered_lanes(self.s):  # stream down: the timers come back
                    now = time.time()
                    jobs.ensure(f"lane_scope:{spec.name}", now, max_attempts=self.s.job_max_attempts, priority=job_priority(self.s, spec))
                    jobs.ensure(f"lane_review:{spec.name}", now + 15, max_attempts=self.s.job_max_attempts, priority=job_priority(self.s, spec))
            return
        if item.get("event") not in {"created", "activated", "close_date_updated", "strike", "determined", "settled"}:
            return
        series = str(item.get("ticker", "")).split("-")[0]
        for spec in tiered_lanes(self.s):
            if self._driven(spec) and series in runner.lane_series(spec, self.s):
                jobs.run_now(f"lane_scope:{spec.name}", priority=job_priority(self.s, spec))

    def _tiered_spec(self, job: Job):
        spec = lane_specs(self.s).get(job.kind.partition(":")[2])
        return spec if spec is not None and spec.enabled and tiered(spec) else None

    def do_lane_scope(self, job: Job) -> str:
        spec = self._tiered_spec(job)
        if spec is None:
            return "lane off or not two-level; not rescheduled"
        if not self._driven(spec):  # a lane the stream drives is scoped by its market events, not by a timer (user, 2026-11-19: "nothing should be running off a timer unless its a background job for database calculations")
            self.jobs.ensure(
                job.kind, time.time() + (spec.scope_minutes or 30.0) * 60,
                max_attempts=self.s.job_max_attempts, priority=job_priority(self.s, spec),
            )
        if spec.name in POLY_LANES and self._poly() is None:
            return "polymarket paper lane has no client"
        venue = self._venue(spec.name)
        with shared_for(venue, self.s.discovery_max_age, self.s.discovery_enabled):
            n = runner.scope_lane(self.s, venue, self.store, spec, quant=None if spec.name in POLY_LANES else self.quant)
        self.jobs.run_now(f"lane_review:{spec.name}", priority=job_priority(self.s, spec))
        log.info("scope %s: watching %d event(s)", spec.name, n)
        return f"{n} events watched"

    def do_lane_review(self, job: Job) -> str:
        spec = self._tiered_spec(job)
        if spec is None:
            return "lane off or not two-level; not rescheduled"
        every = (spec.review_minutes or 2.0) * 60
        if not self._driven(spec):  # a lane the stream drives is reviewed on a socket quote or an index tick, not by a timer
            self.jobs.ensure(
                job.kind, time.time() + every, max_attempts=self.s.job_max_attempts,
                priority=job_priority(self.s, spec),
            )
        if spec.name in POLY_LANES and self._poly() is None:
            return "polymarket paper lane has no client"
        venue = self._venue(spec.name)
        with shared_for(venue, self.s.discovery_max_age, self.s.discovery_enabled):
            shots = runner.review_lane(self.s, venue, self.store, spec, self.estimators())
        queued = 0
        for shot in shots:
            made = self.jobs.enqueue(
                "evaluate", {"shot": asdict(shot)}, dedupe_key=f"evaluate:{shot.lane}:{shot.ticker}",
                expires_in=max(60.0, 2 * every), max_attempts=self.s.job_max_attempts,
                priority=job_priority(self.s, spec),
            )
            queued += 1 if made else 0
        return f"{len(self.store.watch(spec.name))} watched, queued {queued} of {len(shots)}"

    def estimators(self) -> dict:
        return build_estimators(self.est, self.store, self.searcher, self.quant)

    def do_resolve(self, job: Job) -> str:
        n = runner.resolve(self.client, self.store, client_for=lambda ticker: self._venue(self.store.decision_lane(ticker)))
        log.info("resolved %d settled markets", n)
        return f"{n} resolved"

    def do_account_sync(self, job: Job) -> str:
        """Keep a local copy of your fills and settlements, for analysis. Read-only, low priority."""
        from . import account

        if not self.s.account_sync_enabled:
            return "account sync off"
        got = account.sync(self.client, self.store)
        wait = 120 if got["markets_left"] else self.s.account_sync_minutes * 60  # a first sync looks markets up a few at a time
        self.jobs.ensure(
            "account_sync", time.time() + wait, max_attempts=self.s.job_max_attempts, priority=HISTORY_PRIORITY
        )
        return f"{got['fills']} fills, {got['settlements']} settlements, {got['markets']} markets looked up"

    def do_patterns(self, job: Job) -> str:
        """Rewrite data/patterns.md: what your larger wins and losses have in common. Read-only, low priority."""
        from . import patterns

        if self.s.patterns_hours <= 0:
            return "patterns off"
        report = patterns.analyze(self.store)
        (self.s.data_dir / "patterns.md").write_text(patterns.render(report))
        self.jobs.ensure(
            "patterns", time.time() + self.s.patterns_hours * 3600, max_attempts=self.s.job_max_attempts,
            priority=HISTORY_PRIORITY,
        )
        return "patterns written" if report else "not enough trades yet"

    def do_history(self, job: Job) -> str:
        """Download candles for newly settled 15-minute windows. Low priority: trading jobs go first."""
        from . import history
        from .commodities import load_instruments

        s = self.s
        if not s.history_enabled:
            return "history off"
        from .kalshiws import crypto_15m_series

        series = list(dict.fromkeys([f"{i.prefix}15M" for i in load_instruments(s.data_dir) if "15M" in i.suffixes] + crypto_15m_series()))
        added = history.ingest(
            self.client, self.store, series, windows=200, max_new=s.history_batch, refresh=True
        )
        pruned = history.prune(self.store, s.history_keep_days)
        # A full batch means more is waiting (the first run has days to catch up on): come back soon.
        wait = 120 if added >= s.history_batch else s.history_interval_minutes * 60
        self.jobs.ensure(
            "history", time.time() + wait, max_attempts=s.job_max_attempts, priority=HISTORY_PRIORITY
        )
        return f"{added} windows added, {pruned} old removed"

    def do_review(self, job: Job) -> str:
        from .review import run_review

        if not self.s.review_enabled:
            self._schedule_review()
            return "review off"
        notes = run_review(self.s, self.store)
        for line in notes:
            log.info("%s", line)
        self.refresh()
        self._schedule_review()
        return notes[0] if notes else "no change"

    def _schedule_review(self) -> None:
        every = self.s.review_interval_minutes * 60
        self.jobs.ensure("review", time.time() + every, max_attempts=self.s.job_max_attempts)
