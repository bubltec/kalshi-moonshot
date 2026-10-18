"""Commands: scan | run | resolve | report [--all-versions] | prune [--yes] | jobs [retry] | balance | kill | unkill | doctor"""

from __future__ import annotations

import logging
import signal
import sys

from . import config, report, runner
from .jobs import Jobs
from .kalshi import KalshiClient, KalshiError
from .llm import Ollama
from .search import NewsSearch
from .store import STRATEGY_VERSION, Store
from .worker import Worker

log = logging.getLogger("moonshot")


def _client(s: config.Settings) -> KalshiClient:
    return KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps)


def _print_scan(results: list[runner.Outcome]) -> None:
    for r in results:
        print(runner.format_outcome(r))


def cmd_scan(s: config.Settings) -> int:
    est = Ollama(s.ollama_url, s.model)
    if not est.ping():
        print(f"Ollama is not reachable at {s.ollama_url}. Start it, or run `doctor`.")
        return 1
    searcher = NewsSearch(s.search_results) if s.search_enabled else None
    results = runner.scan(s, _client(s), est, Store(s.db_path, s.exclude_versions), searcher)
    _print_scan(results)
    print(f"\n{len(results)} evaluated, mode={s.mode}, env={s.env}")
    return 0


def cmd_run(s: config.Settings) -> int:
    """The loop: a persistent job queue (see worker.py). Safe to stop and start at any time."""
    store = Store(s.db_path, s.exclude_versions)
    worker = Worker(
        s, _client(s), Ollama(s.ollama_url, s.model), store,
        NewsSearch(s.search_results),  # one instance so its cache survives between scans
        client_factory=_client,
        est_factory=lambda fresh: Ollama(fresh.ollama_url, fresh.model),
        reload=config.reload,
    )
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: worker.stop())  # finish the current job, then exit
    log.info("loop start: env=%s mode=%s real_money=%s", s.env, s.mode, s.real_money)
    worker.run_forever()
    return 0


def cmd_jobs(s: config.Settings) -> int:
    """Queue status. `jobs retry` requeues dead jobs (never ones that were mid-order)."""
    jobs = Jobs(Store(s.db_path, s.exclude_versions))
    if "retry" in sys.argv:
        n, skipped = jobs.retry_dead()
        print(f"requeued {n} dead job(s)")
        if skipped:
            print(f"left {skipped} alone: they were mid-order. Check Kalshi, then clear them by hand.")
        return 0
    counts = jobs.counts()
    print("kind       status     count")
    for kind, status, n in counts:
        print(f"{kind:10} {status:10} {n:5}")
    if not counts:
        print("(no jobs yet)")
    dead = jobs.dead()
    if dead:
        print("\nrecent dead jobs:")
        for r in dead:
            print(f"  #{r['id']} {r['kind']} after {r['attempts']} tries: {r['last_error']}")
    return 0


def cmd_resolve(s: config.Settings) -> int:
    print(f"resolved {runner.resolve(_client(s), Store(s.db_path, s.exclude_versions))} markets")
    return 0


def cmd_report(s: config.Settings) -> int:
    store = Store(s.db_path, s.exclude_versions)
    every = "--all-versions" in sys.argv
    rows = store.settled_decisions(include_excluded=every)
    print("decisions by strategy version (current is " + STRATEGY_VERSION + "):")
    for v in store.version_counts():
        left_out = "" if every or v["version"] not in s.exclude_versions else "  <- excluded from this report"
        print(f"  {v['version']:6} {v['decisions']:5} logged, {v['settled'] or 0:4} settled{left_out}")
    print()

    def show(title: str, summary: dict) -> None:
        print(f"== {title} (each market counted once)")
        for k, v in summary.items():
            print(f"{k:28} {v:.4f}" if isinstance(v, float) else f"{k:28} {v}")
        if summary["settled"] < 30:
            print("Fewer than 30 settled markets: too early to say whether there is an edge.")
        print()

    show("all lanes", report.summarize(rows))
    for lane, summary in report.by_lane(rows).items():
        show(f"lane: {lane}", summary)
    for category, summary in report.by_category(rows).items():
        show(f"category: {category}", summary)
    return 0


def cmd_prune(s: config.Settings) -> int:
    """Delete decisions of the excluded versions. Dry run unless --yes."""
    if not s.exclude_versions:
        print("EXCLUDE_VERSIONS is empty, so there is nothing to prune.")
        return 0
    apply = "--yes" in sys.argv
    result = Store(s.db_path, s.exclude_versions).prune(s.exclude_versions, dry_run=not apply)
    print(f"versions: {', '.join(sorted(s.exclude_versions))}")
    print(f"decisions in those versions:        {result['matched']}")
    print(f"kept (we hold orders on the ticker): {result['kept_for_orders']}")
    if apply:
        print(f"deleted:                            {result['deleted']}")
        if "backup" in result:
            print(f"backup of the old database:         {result['backup']}")
    else:
        print("Dry run. Nothing deleted. Re-run with --yes to delete (a backup is made first).")
    return 0


def cmd_balance(s: config.Settings) -> int:
    print(_client(s).balance())
    return 0


def cmd_kill(s: config.Settings) -> int:
    s.kill_file.write_text("stop\n")
    print(f"kill switch on ({s.kill_file}). No orders will be placed until `unkill`.")
    return 0


def cmd_unkill(s: config.Settings) -> int:
    s.kill_file.unlink(missing_ok=True)
    print("kill switch off")
    return 0


def cmd_doctor(s: config.Settings) -> int:
    print(f"env={s.env} mode={s.mode} real_money={s.real_money} base={s.base_url}")
    c = _client(s)
    try:
        n = sum(1 for _ in c.open_markets(pages=1))
        print(f"public market data: ok ({n} markets on page 1)")
    except KalshiError as exc:
        print(f"public market data: FAILED {exc}")
    if c.authed:
        try:
            print(f"balance: {c.balance()}")
        except KalshiError as exc:
            print(f"signed call: FAILED {exc}")
    else:
        print("credentials: not set (fine for observe mode)")
    o = Ollama(s.ollama_url, s.model)
    print(f"ollama {s.ollama_url} model={s.model}: {'ok' if o.ping() else 'NOT REACHABLE'}")
    print(f"kill switch: {'ON' if s.kill_file.exists() else 'off'}")
    return 0


COMMANDS = {
    "scan": cmd_scan, "run": cmd_run, "resolve": cmd_resolve, "report": cmd_report, "prune": cmd_prune, "jobs": cmd_jobs,
    "balance": cmd_balance, "kill": cmd_kill, "unkill": cmd_unkill, "doctor": cmd_doctor,
}


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print(__doc__)
        return 2
    # reload() re-reads .env, so one-off commands see edits made after the container started.
    return COMMANDS[argv[1]](config.reload())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
