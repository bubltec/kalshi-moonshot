"""The `run` loop: pulls due jobs from the queue and runs them one at a time.

scan      fetch the markets in the window, shortlist both lanes, queue one `evaluate` per market,
          and make sure the next scan is scheduled (so a crash never ends the loop)
evaluate  re-read the market's current price, ask the model, log the decision, maybe order
resolve   record the outcome of markets that settled
stop_loss sell (or paper-sell) open bets that are under 1h from close and down 30%
review    once an hour, tighten a setting when the scorecard makes that clear

Stop it at any time. Finished work stays finished; running work is picked up again on start.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any

from . import runner
from .commodities import CommodityPricer
from .config import Settings
from .estimators import EstimateShare, build_estimators
from .jobs import PLACING, Job, Jobs
from .kalshi import KalshiClient, KalshiRateLimited
from .marketdata import Discovery, shared_for
from .lanes import lane_specs, spec_for, tiered, tiered_lanes
from .search import NewsSearch
from .store import Store
from .strategy import LongShot, ask_trend, falling_reason, lane_reject_reason, side_view

log = logging.getLogger("moonshot")
# Jobs that are long and not needed to trade (downloading history, resolving settled markets, syncing fills, the broad scan, the hourly review). They run on their own thread,
# so a 40-second job can never hold up a live lane's review or evaluation, and their requests are throttled to a third of the budget.
MAINTENANCE = frozenset({"scan", "resolve", "review", "history", "account_sync", "patterns"})
HISTORY_PRIORITY = -10  # runs only when no trading job is waiting (a running job is never interrupted)


def _price(m: dict, key: str) -> float:
    try:
        return float(m.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def knobs(s: Settings) -> tuple:
    """Settings whose change is worth a log line."""
    return (
        s.env, s.mode, tuple(sorted(s.live_lanes or ())), s.fast_live, s.real_money, s.min_order_usd, s.min_price, s.max_price, s.min_volume, s.max_spread,
        s.min_hours_to_close, s.min_edge, s.scan_pages, s.shortlist, s.scan_interval_minutes,
        s.model, tuple(sorted(s.skip_categories)), s.skip_numeric, tuple(sorted(s.exclude_versions)),
        s.max_rps, s.page_size, s.job_max_attempts, s.moonshot_max_drop,
        s.stop_loss_enabled, s.stop_loss_hours, s.stop_loss_drop,
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
        self.discovery: Any = None
        self.stopping = False
        self._last_cleanup = 0.0
        self.share = EstimateShare()  # one estimate per market, reused by every lane that asks soon after
        self._split = False  # True once run_forever has started the maintenance thread: the main loop then leaves those jobs to it

    # ---- lifecycle ----
    def start(self) -> None:
        """Recover from the last run, and scan right away (a restart is how you force a re-run)."""
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
        from . import backup

        backup.start_nightly(lambda: self.s)
        from . import archive
        from .tape import Tape

        archive.start_nightly(
            lambda: self.s, lambda: Store(self.s.db_path, self.s.exclude_versions), lambda: Tape(self.s.tape_path).db
        )  # idle until ARCHIVE_BUCKET is set

    def _start_tape(self) -> None:
        """Record quotes next to the feed price they follow (read-only; see tape.py)."""
        if not self.s.tape_enabled or not getattr(self.client, "authed", False) or self.recorder is not None:
            return
        from .tape import TapeRecorder

        try:
            self.recorder = TapeRecorder(self.client, self.s, getattr(self.quant, "feed", None))
            self.recorder.start()
            log.info("tape recorder on: %s (kept %.0f days)", self.s.tape_path, self.s.tape_keep_days)
            self._start_watcher()
            self._start_entry_watcher()
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
        self.recorder.extra_tickers = lambda: self._held_for_watching() | w.manual_tickers()  # hourly, daily and hand-placed positions are streamed too
        w.start()
        self.watcher = w
        log.info("take-profit watcher on: selling from the live socket when a lane's level is hit")

    def _start_entry_watcher(self) -> None:
        """Re-review a watched event the moment a streamed quote enters a lane's price band (only lanes with entry_socket = true)."""
        from . import entrywatch

        if self.recorder is None or self.entry_watcher is not None or not entrywatch.wanted(self.s):
            return
        db_path = self.s.db_path
        w = entrywatch.EntryWatcher(
            lambda: self.s, self.client, lambda: Store(db_path, self.s.exclude_versions),
            lambda store: build_estimators(self.est, store, self.searcher, self.quant),
        )
        self.recorder.listeners.append(w.on_event)
        w.start()
        self.entry_watcher = w
        log.info("entry watcher on: reviewing a watched event as soon as its socket quote enters the lane's price band")

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
        """Tickers of open bets whose lane has a take-profit or peak-crash rule: the socket should stream them."""
        from . import lanes

        specs = lanes.lane_specs(self.s)
        out = set()
        for row in self.store.open_bets():
            spec = specs.get(row["lane"] or "moonshot")
            stop_on = spec is not None and self.s.stop_loss_enabled and spec.stop_loss and not (
                row["ticker"].split("-")[0].endswith("15M") and not self.s.stop_loss_fast)
            if spec is not None and (lanes.takes_profit(spec) or stop_on):
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
        )
        self.discovery.start()

    def _shared(self, max_age: float) -> Any:
        """Reads of listings and markets inside this block may come from the shared cache (a no-op when discovery is off or the client has no cache)."""
        return shared_for(self.client, max_age, self.s.discovery_enabled)

    def stop(self) -> None:
        self.stopping = True
        if self.discovery is not None:
            self.discovery.stop()
        if self.watcher is not None:
            self.watcher.stop()
        if self.entry_watcher is not None:
            self.entry_watcher.stop()
        if self.recorder is not None:
            self.recorder.stop()

    def run_forever(self) -> None:
        self.start()
        log.info("edits to .env apply on the next job; no rebuild. Stop with ctrl-c or `docker compose stop`.")
        self._split = True
        threading.Thread(target=self._maintenance_loop, name="maintenance", daemon=True).start()
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

    def step(self) -> bool:
        """Claim and run one due job. Returns False when nothing was due."""
        self.refresh()
        self._housekeeping()
        job = self.jobs.claim(exclude=MAINTENANCE if self._split else None)
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
        if (
            self._client_factory
            and (
                fresh.base_url != s.base_url or fresh.key_id != s.key_id
                or fresh.private_key_path != s.private_key_path or fresh.max_rps != s.max_rps
                or fresh.page_size != s.page_size
            )
        ):
            self.client._http.close()
            self.client = self._client_factory(fresh)
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
        if self.searcher:
            self.searcher.results = fresh.search_results

    # ---- running a job ----
    def run_job(self, job: Job) -> None:
        handlers = {
            "scan": self.do_scan, "evaluate": self.do_evaluate,
            "resolve": self.do_resolve, "stop_loss": self.do_stop_loss,
            "review": self.do_review, "history": self.do_history, "account_sync": self.do_account_sync, "patterns": self.do_patterns,
        }
        try:
            handler = handlers.get(job.kind)
            if handler is None and job.kind.startswith(("lane_scope:", "lane_review:")):
                handler = self.do_lane_scope if job.kind.startswith("lane_scope:") else self.do_lane_review
            if handler is None:
                raise ValueError(f"unknown job kind {job.kind!r}")
            self.jobs.complete(job.id, handler(job) or "")
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

    def do_scan(self, job: Job) -> str:
        s = self.s
        every = s.scan_interval_minutes * 60
        # Scheduled first, so even a scan that crashes or is killed leaves the next one waiting.
        self.jobs.ensure("scan", time.time() + every, max_attempts=s.job_max_attempts)
        markets = runner.candidate_markets(s, self.client, partial_ok=job.last_attempt)
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
                priority=spec_for(s, shot.lane).priority,
            )
            queued += 1 if made else 0
        self.jobs.enqueue("resolve", dedupe_key="resolve", max_attempts=s.job_max_attempts)
        self._schedule_stop_loss()
        self._ensure_tiered()
        return f"{len(markets)} markets, queued {queued} of {len(shots)} evaluations"

    def do_evaluate(self, job: Job) -> str:
        """A live lane's evaluation always reads the real market (it is about to place an order); a paper lane's may use the shared cache."""
        live = self.s.is_live(job.payload["shot"].get("lane", ""))
        with self._shared(0.0 if live else self.s.discovery_eval_age):
            return self._evaluate(job)

    def _evaluate(self, job: Job) -> str:
        s = self.s
        shot = LongShot(**job.payload["shot"])
        # The shortlist may be minutes old. Re-read the market and drop it if it no longer fits.
        m = self.client.market(shot.ticker)
        m["category"] = shot.category
        if shot.series:
            m.setdefault("series_ticker", shot.series)
        now = datetime.now(UTC)
        spec = spec_for(s, shot.lane)
        m.setdefault("event_ticker", shot.event_ticker)
        why_not = lane_reject_reason(side_view(m, shot.side), spec, s, now)
        if not why_not and spec.trend == "not_falling":
            try:
                candles = self.client.candlesticks(shot.series, shot.ticker, spec.lookback_hours)
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
        shot = replace(shot, bid=_price(view, "yes_bid_dollars"), ask=_price(view, "yes_ask_dollars"))
        # Same job, same order id: if the process dies after sending, a rerun cannot buy twice.
        order_key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"moonshot/{s.env}/{job.created_ts}/{job.id}"))
        out = runner.evaluate(
            s, self.client, self.est, self.store, shot, self.searcher,
            quant=self.quant, estimators=self.estimators(), share=self.share,
            raise_model_errors=not job.last_attempt,
            order_key=order_key,
            before_order=lambda: self.jobs.checkpoint(job.id, PLACING),
        )
        print(runner.format_outcome(out), flush=True)
        if out.action == "bet":
            self._schedule_stop_loss()
            self._watch_new_position(out.ticker)
        return f"{out.action} {out.ticker}"

    # ---- two-level lanes: a slow scoping pass feeds a fast reviewing pass ----
    def _ensure_tiered(self) -> None:
        """Keep one scope and one review job alive per two-level lane (also picks up a lane
        turned on by a settings change)."""
        for spec in tiered_lanes(self.s):
            now = time.time()
            self.jobs.ensure(
                f"lane_scope:{spec.name}", now, max_attempts=self.s.job_max_attempts,
                priority=spec.priority,
            )
            self.jobs.ensure(
                f"lane_review:{spec.name}", now + 15, max_attempts=self.s.job_max_attempts,
                priority=spec.priority,
            )

    def _tiered_spec(self, job: Job):
        spec = lane_specs(self.s).get(job.kind.partition(":")[2])
        return spec if spec is not None and spec.enabled and tiered(spec) else None

    def do_lane_scope(self, job: Job) -> str:
        spec = self._tiered_spec(job)
        if spec is None:
            return "lane off or not two-level; not rescheduled"
        self.jobs.ensure(
            job.kind, time.time() + (spec.scope_minutes or 30.0) * 60,
            max_attempts=self.s.job_max_attempts, priority=spec.priority,
        )
        with self._shared(self.s.discovery_max_age):
            n = runner.scope_lane(self.s, self.client, self.store, spec, quant=self.quant)
        self.jobs.run_now(f"lane_review:{spec.name}", priority=spec.priority)
        log.info("scope %s: watching %d event(s)", spec.name, n)
        return f"{n} events watched"

    def do_lane_review(self, job: Job) -> str:
        spec = self._tiered_spec(job)
        if spec is None:
            return "lane off or not two-level; not rescheduled"
        every = (spec.review_minutes or 2.0) * 60
        self.jobs.ensure(
            job.kind, time.time() + every, max_attempts=self.s.job_max_attempts,
            priority=spec.priority,
        )
        with self._shared(self.s.discovery_max_age):
            shots = runner.review_lane(self.s, self.client, self.store, spec, self.estimators())
        queued = 0
        for shot in shots:
            made = self.jobs.enqueue(
                "evaluate", {"shot": asdict(shot)}, dedupe_key=f"evaluate:{shot.lane}:{shot.ticker}",
                expires_in=max(60.0, 2 * every), max_attempts=self.s.job_max_attempts,
                priority=spec.priority,
            )
            queued += 1 if made else 0
        return f"{len(self.store.watch(spec.name))} watched, queued {queued} of {len(shots)}"

    def estimators(self) -> dict:
        return build_estimators(self.est, self.store, self.searcher, self.quant)

    def do_resolve(self, job: Job) -> str:
        n = runner.resolve(self.client, self.store)
        log.info("resolved %d settled markets", n)
        return f"{n} resolved"

    def do_stop_loss(self, job: Job) -> str:
        notes = runner.stop_loss_pass(
            self.s, self.client, self.store,
            before_order=lambda: self.jobs.checkpoint(job.id, PLACING),
        )
        for line in notes:
            log.info("%s", line)
        self._schedule_stop_loss()
        return f"{len(notes)} stop-loss action(s)"

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
        series = [f"{i.prefix}15M" for i in load_instruments(s.data_dir) if "15M" in i.suffixes]
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

    def _schedule_stop_loss(self) -> None:
        nxt = runner.next_stop_loss_at(self.s, self.store)
        if nxt is None:
            return
        self.jobs.ensure("stop_loss", nxt, max_attempts=self.s.job_max_attempts)
