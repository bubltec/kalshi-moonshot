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

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any

from . import runner
from .commodities import CommodityPricer
from .config import Settings
from .estimators import build_estimators
from .jobs import PLACING, Job, Jobs
from .kalshi import KalshiClient, KalshiRateLimited
from .lanes import lane_specs, spec_for, tiered, tiered_lanes
from .search import NewsSearch
from .store import Store
from .strategy import LongShot, ask_trend, falling_reason, lane_reject_reason, side_view

log = logging.getLogger("moonshot")
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
        self.stopping = False
        self._last_cleanup = 0.0

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
        self._start_tape()

    def _start_tape(self) -> None:
        """Record quotes next to the feed price they follow (read-only; see tape.py)."""
        if not self.s.tape_enabled or not getattr(self.client, "authed", False) or self.recorder is not None:
            return
        from .tape import TapeRecorder

        try:
            self.recorder = TapeRecorder(self.client, self.s, getattr(self.quant, "feed", None))
            self.recorder.start()
            log.info("tape recorder on: data/tape.db (kept %.0f days)", self.s.tape_keep_days)
            self._start_watcher()
        except Exception:
            log.exception("could not start the tape recorder; continuing without it")
            self.recorder = None

    def _start_watcher(self) -> None:
        """Sell on the live socket when a lane's take-profit level is hit (only if some lane asks for one)."""
        from . import watcher

        if self.recorder is None or self.watcher is not None or not watcher.wanted(self.s):
            return
        db_path = self.s.db_path
        w = watcher.ProfitWatcher(lambda: self.s, self.client, lambda: Store(db_path, self.s.exclude_versions))
        self.recorder.listeners.append(w.on_event)
        w.start()
        self.watcher = w
        log.info("take-profit watcher on: selling from the live socket when a lane's level is hit")

    def stop(self) -> None:
        self.stopping = True
        if self.watcher is not None:
            self.watcher.stop()
        if self.recorder is not None:
            self.recorder.stop()

    def run_forever(self) -> None:
        self.start()
        log.info("edits to .env apply on the next job; no rebuild. Stop with ctrl-c or `docker compose stop`.")
        while not self.stopping:
            if not self.step():
                time.sleep(1.0)
        log.info("stopped cleanly")

    def step(self) -> bool:
        """Claim and run one due job. Returns False when nothing was due."""
        self.refresh()
        self._housekeeping()
        job = self.jobs.claim()
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
                "evaluate", {"shot": asdict(shot)}, dedupe_key=f"evaluate:{shot.ticker}",
                expires_in=every, max_attempts=s.job_max_attempts,
                priority=spec_for(s, shot.lane).priority,
            )
            queued += 1 if made else 0
        self.jobs.enqueue("resolve", dedupe_key="resolve", max_attempts=s.job_max_attempts)
        self._schedule_stop_loss()
        self._ensure_tiered()
        return f"{len(markets)} markets, queued {queued} of {len(shots)} evaluations"

    def do_evaluate(self, job: Job) -> str:
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
            quant=self.quant, estimators=self.estimators(),
            raise_model_errors=not job.last_attempt,
            order_key=order_key,
            before_order=lambda: self.jobs.checkpoint(job.id, PLACING),
        )
        print(runner.format_outcome(out), flush=True)
        if out.action == "bet":
            self._schedule_stop_loss()
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
        shots = runner.review_lane(self.s, self.client, self.store, spec, self.estimators())
        queued = 0
        for shot in shots:
            made = self.jobs.enqueue(
                "evaluate", {"shot": asdict(shot)}, dedupe_key=f"evaluate:{shot.ticker}",
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
