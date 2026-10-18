"""The `run` loop: pulls due jobs from the queue and runs them one at a time.

scan      fetch the markets in the window, shortlist both lanes, queue one `evaluate` per market,
          and make sure the next scan is scheduled (so a crash never ends the loop)
evaluate  re-read the market's current price, ask the model, log the decision, maybe order
resolve   record the outcome of markets that settled

Stop it at any time. Finished work stays finished; running work is picked up again on start.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime

from . import runner
from .config import Settings
from .jobs import PLACING, Job, Jobs
from .kalshi import KalshiClient, KalshiRateLimited
from .search import NewsSearch
from .store import Store
from .strategy import LongShot, reject_reason, steady_reject_reason

log = logging.getLogger("moonshot")


def _price(m: dict, key: str) -> float:
    try:
        return float(m.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def knobs(s: Settings) -> tuple:
    """Settings whose change is worth a log line."""
    return (
        s.env, s.mode, s.real_money, s.min_price, s.max_price, s.min_volume, s.max_spread,
        s.min_hours_to_close, s.min_edge, s.scan_pages, s.shortlist, s.scan_interval_minutes,
        s.model, tuple(sorted(s.skip_categories)), s.skip_numeric, tuple(sorted(s.exclude_versions)),
        s.max_rps, s.job_max_attempts,
        s.steady_enabled, s.steady_min_price, s.steady_max_price, s.steady_min_trend,
        s.steady_max_hours_to_close, s.steady_min_edge, s.steady_usd_per_trade,
        s.search_enabled, s.search_results,
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
    ):
        self.s, self.client, self.est, self.store, self.searcher = s, client, est, store, searcher
        self.jobs = Jobs(store)
        self._client_factory, self._est_factory, self._reload = client_factory, est_factory, reload
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

    def stop(self) -> None:
        self.stopping = True

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
        handlers = {"scan": self.do_scan, "evaluate": self.do_evaluate, "resolve": self.do_resolve}
        try:
            handler = handlers.get(job.kind)
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
        moon, steady = runner.shortlist(s, self.client, markets)
        log.info(runner.describe_scan(s, len(markets), len(moon), len(steady), self.searcher))
        queued = 0
        for shot in moon + steady:
            made = self.jobs.enqueue(
                "evaluate", {"shot": asdict(shot)}, dedupe_key=f"evaluate:{shot.ticker}",
                expires_in=every, max_attempts=s.job_max_attempts,
            )
            queued += 1 if made else 0
        self.jobs.enqueue("resolve", dedupe_key="resolve", max_attempts=s.job_max_attempts)
        return f"{len(markets)} markets, queued {queued} of {len(moon) + len(steady)} evaluations"

    def do_evaluate(self, job: Job) -> str:
        s = self.s
        shot = LongShot(**job.payload["shot"])
        # The shortlist may be minutes old. Re-read the market and drop it if it no longer fits.
        m = self.client.market(shot.ticker)
        m["category"] = shot.category
        if shot.series:
            m.setdefault("series_ticker", shot.series)
        now = datetime.now(UTC)
        why_not = (
            steady_reject_reason(m, s, now) if shot.lane == "steady" else reject_reason(m, s, now)
        )
        if why_not:
            log.info("skip %s: no longer qualifies (%s)", shot.ticker, why_not)
            return f"skipped: {why_not}"
        shot = replace(shot, bid=_price(m, "yes_bid_dollars"), ask=_price(m, "yes_ask_dollars"))
        # Same job, same order id: if the process dies after sending, a rerun cannot buy twice.
        order_key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"moonshot/{s.env}/{job.created_ts}/{job.id}"))
        out = runner.evaluate(
            s, self.client, self.est, self.store, shot, self.searcher,
            raise_model_errors=not job.last_attempt,
            order_key=order_key,
            before_order=lambda: self.jobs.checkpoint(job.id, PLACING),
        )
        print(runner.format_outcome(out), flush=True)
        return f"{out.action} {out.ticker}"

    def do_resolve(self, job: Job) -> str:
        n = runner.resolve(self.client, self.store)
        log.info("resolved %d settled markets", n)
        return f"{n} resolved"
