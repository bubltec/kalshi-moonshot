"""Commands: scan | run | resolve | report [--all-versions] | show-buys | review | prune [--yes] | jobs [retry] | balance | kill | unkill | doctor | lanes"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import UTC, datetime

from . import config, lanes, report, runner
from .commodities import CommodityPricer
from .jobs import Jobs
from .kalshi import KalshiClient, KalshiError
from .llm import Ollama
from .search import NewsSearch
from .store import STRATEGY_VERSION, Store
from .worker import Worker

log = logging.getLogger("moonshot")


def _client(s: config.Settings) -> KalshiClient:
    return KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)


def _print_scan(results: list[runner.Outcome]) -> None:
    for r in results:
        print(runner.format_outcome(r))


def cmd_scan(s: config.Settings, args: argparse.Namespace) -> int:
    est = Ollama(s.ollama_url, s.model)
    if not est.ping():
        print(f"Ollama is not reachable at {s.ollama_url}. Start it, or run `doctor`.")
        return 1
    searcher = NewsSearch(s.search_results) if s.search_enabled else None
    store = Store(s.db_path, s.exclude_versions)
    client = _client(s)
    results = runner.scan(s, client, est, store, searcher, quant=CommodityPricer())
    _print_scan(results)
    for line in runner.stop_loss_pass(s, client, store):
        print(line)
    print(f"\n{len(results)} evaluated, mode={s.mode}, env={s.env}")
    return 0


def cmd_run(s: config.Settings, args: argparse.Namespace) -> int:
    """The loop: a persistent job queue (see worker.py). Safe to stop and start at any time."""
    store = Store(s.db_path, s.exclude_versions)
    worker = Worker(
        s, _client(s), Ollama(s.ollama_url, s.model), store,
        NewsSearch(s.search_results),  # one instance so its cache survives between scans
        client_factory=_client,
        est_factory=lambda fresh: Ollama(fresh.ollama_url, fresh.model),
        reload=config.reload,
        quant=CommodityPricer(),  # one instance so the price-feed cache survives between scans
    )
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: worker.stop())  # finish the current job, then exit
    log.info("loop start: env=%s mode=%s real_money=%s", s.env, s.mode, s.real_money)
    worker.run_forever()
    return 0


def cmd_jobs(s: config.Settings, args: argparse.Namespace) -> int:
    """Queue status. `jobs retry` requeues dead jobs (never ones that were mid-order)."""
    jobs = Jobs(Store(s.db_path, s.exclude_versions))
    if args.action == "retry":
        n, skipped = jobs.retry_dead()
        print(f"requeued {n} dead job(s)")
        if skipped:
            print(f"left {skipped} alone: they were mid-order. Check Kalshi, then clear them by hand.")
        return 0
    counts = jobs.counts()
    print("kind                   status     count")
    for kind, status, n in counts:
        print(f"{kind:22} {status:10} {n:5}")
    if not counts:
        print("(no jobs yet)")
    dead = jobs.dead()
    if dead:
        print("\nrecent dead jobs:")
        for r in dead:
            print(f"  #{r['id']} {r['kind']} after {r['attempts']} tries: {r['last_error']}")
    return 0


def cmd_resolve(s: config.Settings, args: argparse.Namespace) -> int:
    print(f"resolved {runner.resolve(_client(s), Store(s.db_path, s.exclude_versions))} markets")
    return 0


def format_open_buy(row) -> str:
    """One open position: what filled, and the estimate that cleared the edge bar."""
    when = datetime.fromtimestamp(row["filled_ts"], UTC).strftime("%Y-%m-%d %H:%M UTC")
    ask = row["ask"]
    model = row["model_p"]
    edge = row["edge"]
    numbers = "no bet decision stored next to this fill"
    if model is not None and ask is not None and edge is not None:
        numbers = f"model {model:.2f}   ask {ask:.2f}   edge {edge:.2f}"
    headlines = (row["context"] or "").strip() or "(no headlines stored)"
    title = (row["title"] or "").strip()
    lines = [
        row["ticker"],
        f"  {title}" if title else "",
        f"  {row['contracts']:.0f} contracts at {row['price']:.2f} for ${row['cost_usd']:.2f} on {when}",
        f"  {numbers}",
        "  headlines:",
        *(f"  {line}" for line in headlines.splitlines()),
    ]
    return "\n".join(line for line in lines if line != "")


def cmd_show_buys(s: config.Settings, args: argparse.Namespace) -> int:
    """Open real buys, with the probability, edge, and headlines from the fill."""
    rows = Store(s.db_path, s.exclude_versions).open_positions(s.env)
    if not rows:
        print(f"no open buys in {s.env}")
        return 0
    spent = sum(r["cost_usd"] or 0 for r in rows)
    print(f"{len(rows)} open buy(s) in {s.env}, ${spent:.2f} filled\n")
    print("\n\n".join(format_open_buy(r) for r in rows))
    return 0


def cmd_report(s: config.Settings, args: argparse.Namespace) -> int:
    store = Store(s.db_path, s.exclude_versions)
    every = args.all_versions
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


def cmd_prune(s: config.Settings, args: argparse.Namespace) -> int:
    """Delete decisions of the excluded versions. Dry run unless --yes."""
    if not s.exclude_versions:
        print("EXCLUDE_VERSIONS is empty, so there is nothing to prune.")
        return 0
    apply = args.yes
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


def cmd_review(s: config.Settings, args: argparse.Namespace) -> int:
    """Run the hourly review once: print what it changed, or that nothing was clear."""
    from .review import run_review

    if not s.review_enabled:
        print("REVIEW_ENABLED is off. Nothing changed.")
        return 0
    for line in run_review(s, Store(s.db_path, s.exclude_versions)):
        print(line)
    return 0


def cmd_lanes(s: config.Settings, args: argparse.Namespace) -> int:
    """Show every lane's parameters, where they come from, and how the record is weighing it."""
    store = Store(s.db_path, s.exclude_versions)
    for spec in lanes.lane_specs(s).values():
        state = "on" if spec.enabled else "OFF"
        print(
            f"{spec.name:10} {state:3} estimator={spec.estimator} ask={spec.price[0]:.2f}-{spec.price[1]:.2f} "
            f"hours={spec.hours[0]:g}-{spec.hours[1]:g} edge>={spec.min_edge} lag<={spec.max_lag} "
            f"usd={spec.usd_per_trade} trust={spec.trust:.2f} "
            f"loops={'scope ' + format(spec.scope_minutes, 'g') + 'm / review ' + format(spec.review_minutes, 'g') + 'm' if spec.review_minutes else 'main scan'}"
        )
        cal = store.calibrator.adjust(spec.name, "", sum(spec.price) / 2, 0.5, spec.trust)
        skill = "n/a" if cal.skill is None else f"{cal.skill:+.2f}"
        print(f"{'':14}record: {cal.n} settled, skill {skill}, weight on estimate {cal.weight:.2f}")
    path = lanes.strategies_path(s.data_dir)
    print(f"\nstrategies file: {path} ({'present' if path.is_file() else 'not present; defaults from .env'})")
    return 0


def cmd_backfill(s: config.Settings, args: argparse.Namespace) -> int:
    """Fill in the category of old decisions by asking Kalshi for each series. Dry run unless --yes."""
    store = Store(s.db_path, s.exclude_versions)
    pending = store.uncategorized_series()
    if not pending:
        print("every decision already has a category")
        return 0
    client = _client(s)
    found: dict[str, str] = {}
    for series in sorted(pending):
        try:
            found[series] = str(client.series(series).get("category") or "").strip()
        except KalshiError as exc:
            print(f"  {series}: lookup failed ({exc})")
    result = store.backfill_categories(found, dry_run=not args.yes)
    print(f"decisions with no category:   {result['rows']} across {len(pending)} series")
    print(f"can be filled from Kalshi:    {result['fillable']}")
    for series, category in sorted(found.items()):
        if category:
            print(f"  {series:28} {pending[series]:4} -> {category}")
    if result["unresolved"]:
        print(f"left unknown (no category from Kalshi): {', '.join(result['unresolved'])}")
    if args.yes:
        print(f"updated:                      {result['updated']}")
        if "backup" in result:
            print(f"backup of the old database:   {result['backup']}")
    else:
        print("Dry run. Nothing changed. Re-run with --yes to write (a backup is made first).")
    return 0


def cmd_arbs(s: config.Settings, args: argparse.Namespace) -> int:
    """Scan open strike ladders for guaranteed-profit pairs (read-only; nothing is ever ordered)."""
    from .strategy import ladder_arbs

    client = _client(s)
    markets = list(client.open_markets(pages=80))
    found = ladder_arbs(markets)
    ladders = len({m["event_ticker"] for m in markets if m.get("ticker", "").rsplit("-", 1)[-1][:1] == "T"})
    print(f"{len(markets)} open markets, {ladders} strike ladders, {len(found)} guaranteed-profit pair(s) after fees")
    for a in found[: args.top]:
        print(
            f"  +{a.net:.3f}/pair  buy YES {a.low}  +  buy NO {a.high}  "
            f"(cost {a.cost:.3f}, pays at least 1.00, depth ~{a.size:.0f})"
        )
    if found:
        print("Both legs must fill. Orders here are immediate-or-cancel, so one leg can fill alone: check the books first.")
    return 0


def cmd_balance(s: config.Settings, args: argparse.Namespace) -> int:
    print(_client(s).balance())
    return 0


def cmd_kill(s: config.Settings, args: argparse.Namespace) -> int:
    s.kill_file.write_text("stop\n")
    print(f"kill switch on ({s.kill_file}). No orders will be placed until `unkill`.")
    return 0


def cmd_unkill(s: config.Settings, args: argparse.Namespace) -> int:
    s.kill_file.unlink(missing_ok=True)
    print("kill switch off")
    return 0


def cmd_doctor(s: config.Settings, args: argparse.Namespace) -> int:
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
    "scan": cmd_scan, "run": cmd_run, "resolve": cmd_resolve, "report": cmd_report,
    "prune": cmd_prune, "jobs": cmd_jobs, "balance": cmd_balance, "kill": cmd_kill,
    "unkill": cmd_unkill, "doctor": cmd_doctor, "review": cmd_review, "show-buys": cmd_show_buys,
    "lanes": cmd_lanes, "backfill": cmd_backfill, "arbs": cmd_arbs,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kalshi-moonshot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        p = sub.add_parser(name, help=(COMMANDS[name].__doc__ or "").strip().splitlines()[0] if COMMANDS[name].__doc__ else None)
        if name == "report":
            p.add_argument("--all-versions", action="store_true", help="include excluded versions")
        if name == "arbs":
            p.add_argument("--top", type=int, default=10, help="how many to list")
        if name in {"prune", "backfill"}:
            p.add_argument("--yes", action="store_true", help="actually delete (a backup is made first)")
        if name == "jobs":
            p.add_argument("action", nargs="?", choices=["retry"], help="requeue dead jobs")
    return parser


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv[1:])
    # reload() re-reads .env, so one-off commands see edits made after the container started.
    return COMMANDS[args.command](config.reload(), args)


def cli() -> None:
    """Console-script entry point."""
    raise SystemExit(main(sys.argv))


if __name__ == "__main__":
    cli()
