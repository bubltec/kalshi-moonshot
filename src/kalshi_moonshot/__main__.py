"""Commands: scan | run | resolve | report [--all-versions] | show-buys | review | prune [--yes] | jobs [retry] | balance | kill | unkill | doctor | lanes"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import UTC, datetime
from typing import Any

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
    bad = lanes.unknown_live_lanes(s)
    if bad:
        log.warning(
            "LIVE_LANES names %s, which %s not a lane (lanes: %s). 15-minute markets are part of the commodity lane: "
            "turn them on with COMMODITY_15M_LIVE=true.", ", ".join(bad), "is" if len(bad) == 1 else "are",
            ", ".join(lanes.lane_specs(s)),
        )
    live = [x.name for x in lanes.enabled_lanes(s) if s.is_live(x.name)]
    log.info(
        "loop start: env=%s mode=%s real_money=%s live lanes: %s", s.env, s.mode, s.real_money,
        ", ".join(live) or "none (paper only)",
    )
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
        state += " LIVE" if spec.enabled and s.is_live(spec.name) else " paper"
        if spec.timed_suffix and s.is_live(spec.name):
            state += " (15m: LIVE)" if s.fast_live else " (15m: paper)"
        print(
            f"{spec.name:10} {state:24} estimator={spec.estimator} ask={spec.price[0]:.2f}-{spec.price[1]:.2f} "
            f"hours={spec.hours[0]:g}-{spec.hours[1]:g} min-return={spec.min_return:.0%} edge>={spec.min_edge} lag<={spec.max_lag} "
            f"usd={spec.usd_per_trade} trust={spec.trust:.2f} "
            f"{'15m-window=' + format(spec.entry_elapsed[0], 'g') + '-' + format(spec.entry_elapsed[1], 'g') + 'min-in ' if spec.timed_suffix else ''}"
            f"loops={'scope ' + format(spec.scope_minutes, 'g') + 'm / review ' + format(spec.review_minutes, 'g') + 'm' if spec.review_minutes else 'main scan'}"
        )
        cal = store.calibrator.adjust(spec.name, "", sum(spec.price) / 2, 0.5, spec.trust)
        skill = "n/a" if cal.skill is None else f"{cal.skill:+.2f}"
        print(f"{'':14}record: {cal.n} settled, skill {skill}, weight on estimate {cal.weight:.2f}")
    for name in lanes.unknown_live_lanes(s):
        print(f"WARNING: LIVE_LANES names {name!r}, which is not a lane. It does nothing; 15-minute markets are set with COMMODITY_15M_LIVE.")
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


def cmd_bankroll(s: config.Settings, args: argparse.Namespace) -> int:
    """Balance sizing works from, open exposure per lane, and what each lane's bets have returned."""
    from .bankroll import paper_balance, paper_pnl

    store = Store(s.db_path, s.exclude_versions)
    paper = paper_balance(store, s)
    bal = paper
    if s.mode == "trade":
        client = _client(s)
        if client.authed:
            bal = store.bankroll.read(client, s)
    print(f"balance source: {bal.source}   available ${bal.available:,.2f}   equity ${bal.equity:,.2f}")
    if bal.source == "paper":
        gain = bal.equity - s.paper_bankroll_usd
        print(f"paper start ${s.paper_bankroll_usd:,.2f}   realized {gain:+,.2f} ({gain / s.paper_bankroll_usd:+.1%})")
    print(f"sizing: {s.kelly_fraction:.0%} of full Kelly, capped per bet and per lane\n")
    pnl = paper_pnl(store, live=bal.source == "kalshi")
    print(f"{'lane':10} {'bet cap':>8} {'open':>8} {'of equity':>10} {'limit':>6}   {'done':>5} {'wins':>5} {'staked':>9} {'returned':>9} {'P&L':>9}")
    for spec in lanes.lane_specs(s).values():
        opened = bal.open_by_lane.get(spec.name, 0.0)
        r = pnl.get(spec.name, {"n": 0, "wins": 0, "staked": 0.0, "returned": 0.0})
        print(
            f"{spec.name:10} {spec.max_bet_pct:>8.1%} ${opened:>7.2f} {opened / bal.equity:>10.1%} "
            f"{spec.max_exposure_pct:>6.0%}   {int(r['n']):>5} {int(r['wins']):>5} "
            f"${r['staked']:>8.2f} ${r['returned']:>8.2f} {r['returned'] - r['staked']:>+9.2f}"
        )
    return 0


def cmd_shadow(s: config.Settings, args: argparse.Namespace) -> int:
    """15-minute markets we only watch: did the cheap side win more often than its price says?"""
    from .shadow import shadow_table

    store = Store(s.db_path, s.exclude_versions)
    rows = store.shadow_rows(settled_only=True)
    total = store.db.execute("SELECT COUNT(*) FROM shadow").fetchone()[0]
    windows = len({r["ticker"] for r in rows})
    print(f"{total} snapshots logged, {len(rows)} settled across {windows} markets")
    if not rows:
        print("Nothing has settled yet. Windows settle every 15 minutes; check back after a few.")
        return 0
    print("Each market counts once per time bucket. ROI = return from buying that side at the quoted price, after fees.")
    print("Markets in a window move together (BTC and ETH, say), so treat n as smaller than it looks.\n")
    print(f"{'mins left':>10} {'YES mid':>11} {'n':>5} {'YES won':>8} {'mkt mid':>8} {'model':>7} {'buy YES':>8} {'buy NO':>8}")
    for c in shadow_table(rows):
        model = "-" if c.model is None else f"{c.model:.3f}"
        flag = "  (thin)" if c.thin else ""
        print(
            f"{c.minutes[0]:>4.0f}-{c.minutes[1]:<4.0f} {c.price[0]:>4.2f}-{min(c.price[1], 1.0):<5.2f} {c.n:>5} "
            f"{c.yes_rate:>8.3f} {c.market:>8.3f} {model:>7} {c.roi_yes:>+8.1%} {c.roi_no:>+8.1%}{flag}"
        )
    return 0


def cmd_daily(s: config.Settings, args: argparse.Namespace) -> int:
    """Day-by-day profit and loss of the bets actually taken, with drawdown and profit factor."""
    from . import journal

    store = Store(s.db_path, s.exclude_versions)
    live = True if args.live else False if args.paper else None
    items = journal.select(
        journal.trades(store), lane=args.lane, live=live, suffix=args.series_suffix
    )
    label = " ".join(x for x in (
        args.lane, "live" if args.live else "paper" if args.paper else "live+paper",
        args.series_suffix and f"*{args.series_suffix}",
    ) if x)
    if not items:
        print(f"no finished bets for: {label}. Bets are counted once their market closes.")
        return 0
    days = journal.by_day(items, args.tz)[-args.days:]
    print(f"{label}; days are {args.tz} dates, a bet counts on the day its market closed\n")
    print(f"{'day':10} {'trades':>6} {'won':>4} {'win%':>5} {'staked':>8} {'P&L':>8} {'ROI':>7} {'best':>7} {'worst':>7}  running")
    run = sum(d.pnl for d in journal.by_day(items, args.tz)[: -args.days]) if len(
        journal.by_day(items, args.tz)) > args.days else 0.0
    for d in days:
        run += d.pnl
        print(
            f"{d.day:10} {d.n:>6} {d.wins:>4} {d.wins / d.n:>5.0%} ${d.staked:>7.2f} {d.pnl:>+8.2f} "
            f"{d.roi:>+7.1%} {d.best:>+7.2f} {d.worst:>+7.2f}  {run:>+8.2f}"
        )
    m = journal.summarize(items, args.tz)
    print(f"\n{m.trades} trades over {m.days} day(s), {m.green_days} profitable. "
          f"Staked ${m.staked:,.2f}, net {m.pnl:+,.2f} ({m.pnl / m.staked:+.1%} of stake).")
    print(f"win rate {m.win_rate:.0%}; average win ${m.avg_win:.2f}, average loss ${m.avg_loss:.2f} "
          f"(payoff {m.payoff:.2f}, so it needs a {m.breakeven_win_rate:.0%} win rate to break even).")
    print(f"profit factor {m.profit_factor:.2f}  expectancy {m.expectancy:+.2f}/trade  "
          f"max drawdown ${m.max_drawdown:.2f}  worst losing streak {m.worst_streak}")
    if m.trades < 30:
        print("Fewer than 30 finished trades: one loss moves every number here. Read it as a log, not a verdict.")
    return 0


def cmd_history(s: config.Settings, args: argparse.Namespace) -> int:
    """Download Kalshi's minute candles for recent settled 15-minute windows (re-runnable)."""
    from . import history
    from .commodities import load_instruments

    store = Store(s.db_path, s.exclude_versions)
    series = [f"{i.prefix}15M" for i in load_instruments(s.data_dir) if "15M" in i.suffixes]
    if args.series:
        series = [x for x in series if x in args.series.split(",")]
    client = _client(s)
    total = 0
    for ser in series:
        n = history.ingest(client, store, [ser], args.windows)
        total += n
        print(f"{ser:15} +{n} windows", flush=True)
    held = store.db.execute("SELECT COUNT(*), COUNT(DISTINCT series) FROM windows").fetchone()
    print(f"added {total}; the history now holds {held[0]} windows across {held[1]} series")
    return 0


def cmd_profile(s: config.Settings, args: argparse.Namespace) -> int:
    """How the 15-minute contracts move through a window and through the day (needs `history`)."""
    from . import history

    store = Store(s.db_path, s.exclude_versions)
    wins = history.load(store, args.series_suffix)
    if args.series:
        wins = [w for w in wins if w.series in args.series.split(",")]
    if len(wins) < 20:
        print(f"{len(wins)} windows stored; run `history` first (it downloads Kalshi's minute candles).")
        return 0
    first = datetime.fromtimestamp(min(w.close_ts for w in wins), UTC).strftime("%Y-%m-%d %H:%M")
    last = datetime.fromtimestamp(max(w.close_ts for w in wins), UTC).strftime("%Y-%m-%d %H:%M")
    print(f"{len(wins)} settled windows, {len({w.series for w in wins})} series, {first} to {last} UTC\n")
    print("PATH through a window (pooled)")
    print(f"{'min in':>6} {'off 50%':>8} {'decided':>8} {'spread':>7}")
    for k, off, dec, spr in history.path(wins):
        print(f"{k:>6} {off:>8.2f} {dec:>8.0%} {spr:>7.2f}")
    print("\nWHEN a window is effectively over (a side at 85%+ that went on to win)")
    print("  " + "  ".join(f"by min {k}: {share:.0%}" for k, share in history.decision_minute(wins)))
    print("\nFAVORITES priced 70-90% at the entry minute: how often they lose, and the swing against a holder")
    print(f"{'min in':>6} {'n':>5} {'avg price':>10} {'lost':>6} {'swing (median)':>15} {'swing (worst 10%)':>18}")
    for k in (6, 8, 9, 10, 11, 12):
        f = history.favorites(wins, k)
        if f:
            print(f"{k:>6} {f.n:>5} {f.avg_price:>10.2f} {f.reversed_:>6.0%} {f.adverse:>15.2f} {f.p10_adverse:>18.2f}")
    print(f"\nBY HOUR of day ({args.tz}), at the 10-minute mark")
    print(f"{'hour':>4} {'windows':>8} {'moved 5-10':>11} {'decided':>8} {'fav lost':>9} {'(n)':>5} {'spread':>7} {'volume 8-12':>12}")
    for h in history.by_hour(wins, args.tz):
        print(f"{int(h['hour']):>4} {int(h['n']):>8} {h['move']:>11.2f} {h['decided']:>8.0%} {h['reversal']:>9.0%} "
              f"{int(h['fav_n']):>5} {h['spread']:>7.2f} {h['volume']:>12,.0f}")
    return 0


def cmd_leadlag(s: config.Settings, args: argparse.Namespace) -> int:
    """Does the Kalshi quote trail its underlying? Reads the tape the bot records (needs a few hours)."""
    import sqlite3

    from . import leadlag
    from .commodities import load_instruments

    path = s.data_dir / "tape.db"
    if not path.is_file():
        print("no tape yet: the bot records it while running (TAPE_ENABLED=true). Check back in a few hours.")
        return 0
    db = sqlite3.connect(str(path))
    n_q = db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
    span = db.execute("SELECT MIN(ts), MAX(ts) FROM quotes").fetchone()
    hours = ((span[1] - span[0]) / 3600) if span[0] else 0.0
    print(f"tape: {n_q:,} quote changes over {hours:.1f} hours\n")
    series = args.series.split(",") if args.series else [f"{i.prefix}15M" for i in load_instruments(s.data_dir) if "15M" in i.suffixes]
    pooled: list[leadlag.Track] = []
    for ser in series:
        tracks = leadlag.build_tracks(db, ser, s.data_dir)
        if not tracks:
            print(f"{ser:15} not enough data yet")
            continue
        pooled += tracks
        corr = leadlag.correlation_by_lag(tracks, tuple(range(-3, 16)))
        valid = {lag: c for lag, c in corr.items() if c == c}
        if not valid:
            print(f"{ser:15} not enough movement yet")
            continue
        peak = max(valid, key=lambda lag: valid[lag])
        resp = {r.horizon: r for r in leadlag.event_study(tracks)}
        print(f"{ser:15} {len(tracks):3} windows, {sum(len(leadlag.jumps(t)) for t in tracks):4} feed jumps, "
              f"quote and feed line up best at {peak:+d}s (corr {valid[peak]:.2f}; + means the quote trails the feed, - means it leads); "
              f"drift after a jump: " + " ".join(f"+{h}s {100 * resp[h].drift:+.2f}c" for h in (3, 10, 30)))
    if pooled:
        print("\nPOOLED trade test: after a jump, buy the side it favors (react in 2 s), sell back after the hold. Cents per contract after fees and the spread.")
        print(f"{'hold':>6} {'trades':>7} {'avg':>8} {'hit':>6} {'control':>8}")
        for r in leadlag.trade_test(pooled):
            print(f"{r.hold:>5}s {r.n:>7} {r.avg_cents:>+7.2f}c {r.hit:>6.0%} {r.control_cents:>+7.2f}c")
        print("\nThe control is the same trade at random moments in random directions: it shows what spread and fees cost anyway. "
              "Only a gap between the two columns is a lead-lag edge.")
    return 0


def cmd_evidence(s: config.Settings, args: argparse.Namespace) -> int:
    """Test buy/sell ideas against the stored 15-minute history: tuned early, judged on later windows."""
    from importlib import resources

    from . import evidence

    store = Store(s.db_path, s.exclude_versions)
    path = s.data_dir / "rules.toml"
    text = path.read_text() if path.is_file() else resources.files("kalshi_moonshot").joinpath("rules.toml").read_text()
    rules = [r for r in evidence.parse_rules(text) if not args.rule or r.name in args.rule.split(",")]
    wins = evidence.load_windows(store, args.series.split(",") if args.series else None)
    if len(wins) < 200:
        print(f"{len(wins)} windows stored; run `history` first (the bot also downloads them in the background).")
        return 0
    train, test = evidence.split_windows(wins, args.split)
    span = lambda ws: f"{datetime.fromtimestamp(ws[0].close_ts, UTC):%m-%d %H:%M} to {datetime.fromtimestamp(ws[-1].close_ts, UTC):%m-%d %H:%M}"  # noqa: E731
    print(f"{len(wins)} windows: {len(train)} to tune on ({span(train)} UTC), {len(test)} held out ({span(test)} UTC)")
    print(f"rules from {path if path.is_file() else 'the packaged rules.toml'}; costs counted: fees on both legs, entries at the ask, exits at the bid\n")
    fmt = lambda st: "      -" if not st.n else f"{st.roi:+7.1%} (n={st.n})"  # noqa: E731
    for rule in rules:
        v = evidence.evaluate(rule, train, test)
        print(f"== {rule.name}: {rule.about}")
        print(f"   {v.tried} setting(s) tried; baseline with no signal: tuned {fmt(v.base_train)}, held out {fmt(v.base_test)}")
        if v.chosen is None:
            print(f"   {v.label}: {v.reason}\n")
            continue
        print(f"   {'setting':44} {'tuned':>17} {'held out':>17} {'90% interval':>17}")
        for r in v.results[: args.top]:
            print(f"   {r.setting.label():44} {fmt(r.train):>17} {fmt(r.test):>17} {r.test.lo:>+7.1%}..{r.test.hi:>+7.1%}")
        print(f"   {v.positive_held_out} of {len(v.results)} settings were positive held out "
              f"(chance alone gives some). Picking the best on the tuning windows -> {v.label}: {v.reason}\n")
    return 0


def cmd_sync(s: config.Settings, args: argparse.Namespace) -> int:
    """Download your fills and settlements from Kalshi into the database (read-only, re-runnable)."""
    from . import account

    store = Store(s.db_path, s.exclude_versions)
    client = _client(s)
    if not client.authed:
        print("needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
        return 1
    got = account.sync(client, store, full=args.full, market_budget=args.markets)
    held = store.db.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM fills").fetchone()
    print(f"+{got['fills']} fills, +{got['settlements']} settlements, {got['markets']} markets looked up "
          f"({got['markets_left']} still to look up; run again)")
    if held[0]:
        print(f"holding {held[0]} fills from {datetime.fromtimestamp(held[1], UTC):%Y-%m-%d} "
              f"to {datetime.fromtimestamp(held[2], UTC):%Y-%m-%d %H:%M} UTC")
    return 0


def cmd_mytrades(s: config.Settings, args: argparse.Namespace) -> int:
    """Your wins and losses, rebuilt from every fill: by day, by market, by how you got in and out."""
    from . import journal, trips

    store = Store(s.db_path, s.exclude_versions)
    allt = trips.build_trips(store)
    if not allt:
        print("no fills stored yet: run `sync` (the bot also does it hourly).")
        return 0
    src = args.source
    done = trips.finished(allt, src, args.series_suffix)
    open_ = [t for t in allt if not t.done and (src in (None, "all") or t.source == src)]
    unreliable = sum(1 for t in allt if t.done and not t.reliable)
    kinds = {k: sum(1 for t in allt if t.done and t.source == k) for k in ("manual", "bot", "mixed")}
    suffix = f", *{args.series_suffix}" if args.series_suffix else ""
    print(f"{len(done)} finished trades counted ({src}{suffix}); all history: {kinds['manual']} manual, "
          f"{kinds['bot']} bot, {kinds['mixed']} mixed; {len(open_)} still open; "
          f"{unreliable} left out (a settlement disagreed with the fills)\n")
    if not done:
        return 0
    items = [t.as_trade() for t in done]
    m = journal.summarize(items, args.tz)
    print(f"{m.trades} trades, {m.wins} won ({m.win_rate:.0%}). Staked ${m.staked:,.2f}, net {m.pnl:+,.2f} "
          f"({m.pnl / m.staked:+.1%}). Average win ${m.avg_win:.2f}, average loss ${m.avg_loss:.2f}; "
          f"profit factor {m.profit_factor:.2f}; expectancy {m.expectancy:+.2f}/trade; "
          f"max drawdown ${m.max_drawdown:.2f}; worst losing streak {m.worst_streak}.\n")
    print(f"{'day':10} {'trades':>6} {'won':>4} {'staked':>8} {'P&L':>8} {'ROI':>7}")
    for d in journal.by_day(items, args.tz)[-args.days:]:
        print(f"{d.day:10} {d.n:>6} {d.wins:>4} ${d.staked:>7.2f} {d.pnl:>+8.2f} {d.roi:>+7.1%}")

    def table(title: str, rows: list[trips.Row], extra: str = "", unit: str = "") -> None:
        if not rows:
            return
        print(f"\n{title}")
        print(f"{'':16} {'n':>4} {'won':>5} {'staked':>8} {'P&L':>8} {'ROI':>7} {extra:>11}")
        for r in rows:
            e = "" if r.extra != r.extra else (f"{r.extra:.2f}" if unit == "price" else f"{r.extra:.0f} min")
            print(f"{r.label:16} {r.n:>4} {r.win_rate:>5.0%} ${r.staked:>7.2f} {r.pnl:>+8.2f} {r.roi:>+7.1%} {e:>11}")

    table("BY MARKET TYPE", trips.by_series(done)[: args.top])
    table("HOW YOU GOT OUT", trips.by_exit(done), "median hold", "min")
    fifteen = [t for t in done if t.series.endswith("15M")]
    if fifteen:
        table("15-MINUTE MARKETS: how far into the window you bought", trips.entry_minute_table(fifteen), "avg paid", "price")
        table("15-MINUTE MARKETS: price paid", trips.entry_price_table(fifteen), "median hold", "min")
        mult = trips.cashout_multiples(fifteen)
        if mult:
            print(f"\n15-MINUTE CASH-OUTS: {len(mult)} sold early; sale price over purchase price: median "
                  f"{mult[len(mult) // 2]:.2f}x, best {mult[-1]:.2f}x, {sum(1 for x in mult if x >= 2)} at 2x or better, "
                  f"{sum(1 for x in mult if x >= 3)} at 3x or better, {sum(1 for x in mult if x < 1)} sold at a loss")
    ranked = sorted(done, key=lambda t: t.pnl)
    for title, group in (("BEST", ranked[::-1][:5]), ("WORST", ranked[:5])):
        print(f"\n{title}")
        for t in group:
            print(f"  {t.pnl:>+7.2f}  {t.ticker:34} {t.side.upper():3} paid {t.entry_price:.2f} -> {t.exit_price:.2f} "
                  f"({t.exit_type}, {t.hold_minutes:.0f} min, {t.source})")
    return 0


def cmd_patterns(s: config.Settings, args: argparse.Namespace) -> int:
    """What your larger wins and larger losses have in common (read-only; the bot also writes data/patterns.md)."""
    from . import patterns

    store = Store(s.db_path, s.exclude_versions)
    print(patterns.render(patterns.analyze(store, args.source), args.source))
    return 0


def cmd_bymin(s: config.Settings, args: argparse.Namespace) -> int:
    """15-minute results of a lane (or all lanes) by how far into the window each bet was placed."""
    from . import journal

    store = Store(s.db_path, s.exclude_versions)
    items = [t for t in journal.trades(store) if args.lane in (None, "all") or t.lane == args.lane]
    rows = journal.by_entry_minute(items)
    if not rows:
        print("no finished 15-minute bets for that lane yet.")
        return 0
    print(f"{args.lane or 'all lanes'}: 15-minute bets by minute of entry\n")
    print(f"{'':14} {'bets':>5} {'won':>5} {'staked':>8} {'P&L':>8} {'ROI':>7}")
    for band, n, won, staked, pnl in rows:
        print(f"{band:14} {n:>5} {won / n:>5.0%} {staked:>8.2f} {pnl:>+8.2f} {pnl / staked:>+7.1%}")
    print("\nSmall groups swing wildly: read it as a log until each band has 30+ bets.")
    return 0


def cmd_patience(s: config.Settings, args: argparse.Namespace) -> int:
    """Replay finished bets as if each had waited for a slightly better price (needs the tape; places nothing)."""
    from . import patience

    tape = s.data_dir / "tape.db"
    if not tape.is_file():
        print("no tape recorded yet (TAPE_ENABLED, kept for TAPE_KEEP_DAYS days).")
        return 0
    store = Store(s.db_path, s.exclude_versions)
    print(patience.render(patience.load(store, tape, args.lane, args.min_price)))
    return 0


def cmd_screened(s: config.Settings, args: argparse.Namespace) -> int:
    """Why a market that passed a lane's filter never got a decision: our chance, the edge and the bar it missed."""
    import time

    store = Store(s.db_path, s.exclude_versions)
    since = time.time() - args.hours * 3600
    rows = store.db.execute(
        """SELECT ts, lane, ticker, side, ask, model_p, edge, reason FROM screen_log
           WHERE ts>=? AND ticker LIKE ? AND (? IS NULL OR lane=?) ORDER BY ts DESC""",
        (since, args.ticker + "%", args.lane, args.lane),
    ).fetchall()
    latest: dict[tuple[str, str], Any] = {}
    for r in rows:
        latest.setdefault((r["ticker"], r["side"]), r)  # newest first, so this is the most recent view
    if not latest:
        print(f"nothing screened out in the last {args.hours:g}h for {args.ticker or 'any market'}"
              " (markets that fail the lane's price, spread or age filters never reach the screen).")
        return 0
    print(f"{len(latest)} candidates the lane's filter let through but the screen dropped (latest view of each):\n")
    for r in list(latest.values())[: args.limit]:
        age = (time.time() - r["ts"]) / 60
        p = "  - " if r["model_p"] is None else f"{r['model_p']:.0%}"
        print(f"{r['ticker']:34} {r['side'].upper():3} ask {r['ask']:.2f}  ours {p:>4}  {age:4.0f}m ago  {r['reason']}")
    return 0


def cmd_target(s: config.Settings, args: argparse.Namespace) -> int:
    """Paper test: the bot's finished 15-minute bets re-scored with a resting sell at 1.5x / 2x / 3x. Places nothing."""
    from . import target

    store = Store(s.db_path, s.exclude_versions)
    shown = False
    for text in args.mult.split(","):
        mult = float(text)
        rows, missing = target.compare(store, mult, args.suffix, lane=args.lane)
        if not rows:
            print(f"{mult:g}x: no finished *{args.suffix} bets with minute candles yet ({missing} without)")
            continue
        stake = sum(r.stake for r in rows)
        actual, alt = sum(r.actual for r in rows), sum(r.with_target for r in rows)
        if not shown:
            print(f"{'sell at':>8} {'bets':>5} {'sold':>5} {'as played':>10} {'with sell':>10} {'difference':>11}")
            shown = True
        print(f"{mult:>7g}x {len(rows):>5} {sum(r.hit for r in rows):>5} {actual:>+10.2f} {alt:>+10.2f} "
              f"{alt - actual:>+11.2f}   ROI {actual / stake:+.1%} -> {alt / stake:+.1%}"
              + (f"  ({missing} bets had no candles)" if missing else ""))
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
    live = [x.name for x in lanes.enabled_lanes(s) if s.is_live(x.name)]
    print(f"live lanes (place real orders): {', '.join(live) or 'none, paper only'}")
    for name in lanes.unknown_live_lanes(s):
        print(f"WARNING: LIVE_LANES names {name!r}, which is not a lane ({', '.join(lanes.lane_specs(s))}). It does nothing.")
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
    "sync": cmd_sync, "mytrades": cmd_mytrades, "target": cmd_target, "screened": cmd_screened, "patterns": cmd_patterns, "patience": cmd_patience, "bymin": cmd_bymin, "evidence": cmd_evidence, "leadlag": cmd_leadlag, "history": cmd_history, "profile": cmd_profile, "daily": cmd_daily, "lanes": cmd_lanes, "backfill": cmd_backfill, "arbs": cmd_arbs, "bankroll": cmd_bankroll, "shadow": cmd_shadow,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kalshi-moonshot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        p = sub.add_parser(name, help=(COMMANDS[name].__doc__ or "").strip().splitlines()[0] if COMMANDS[name].__doc__ else None)
        if name == "report":
            p.add_argument("--all-versions", action="store_true", help="include excluded versions")
        if name == "sync":
            p.add_argument("--full", action="store_true", help="re-read all history, not only what is new")
            p.add_argument("--markets", type=int, default=60, help="market lookups to do this run")
        if name == "mytrades":
            p.add_argument("--source", choices=["manual", "bot", "mixed", "all"], default="all")
            p.add_argument("--series-suffix", help="only series ending in this, e.g. 15M")
            p.add_argument("--days", type=int, default=14)
            p.add_argument("--top", type=int, default=12, help="market types to list")
            p.add_argument("--tz", default="America/New_York")
        if name == "bymin":
            p.add_argument("--lane", help="a lane name, or leave out for all")
        if name == "patience":
            p.add_argument("--lane", default="commodity")
            p.add_argument("--min-price", type=float, default=0.60, help="only bets priced at least this")
        if name == "patterns":
            p.add_argument("--source", choices=["manual", "bot", "mixed", "all"], default="manual")
        if name == "screened":
            p.add_argument("--ticker", default="", help="ticker prefix, e.g. KXBTCD")
            p.add_argument("--lane")
            p.add_argument("--hours", type=float, default=3.0)
            p.add_argument("--limit", type=int, default=30)
        if name == "target":
            p.add_argument("--mult", default="1.5,2,3", help="comma list of sell multiples of the price paid")
            p.add_argument("--suffix", default="15M")
            p.add_argument("--lane", help="only this lane, e.g. late_ticket (default: every lane)")
        if name == "evidence":
            p.add_argument("--rule", help="comma list of rule names to run (default: all)")
            p.add_argument("--series", help="comma list, e.g. KXBTC15M,KXGOLD15M")
            p.add_argument("--split", type=float, default=0.6, help="share of the earliest windows to tune on")
            p.add_argument("--top", type=int, default=5, help="settings to list per rule")
        if name == "leadlag":
            p.add_argument("--series", help="comma list, e.g. KXBTC15M,KXGOLD15M")
        if name == "history":
            p.add_argument("--windows", type=int, default=150, help="settled windows to fetch per series")
            p.add_argument("--series", help="comma list, e.g. KXBTC15M,KXGOLD15M")
        if name == "profile":
            p.add_argument("--series", help="comma list, e.g. KXBTC15M,KXGOLD15M")
            p.add_argument("--series-suffix", default="15M")
            p.add_argument("--tz", default="America/New_York", help="time zone for the by-hour table")
        if name == "daily":
            p.add_argument("--lane", help="only this lane (moonshot, steady, commodity)")
            grp = p.add_mutually_exclusive_group()
            grp.add_argument("--live", action="store_true", help="only bets sent as real orders")
            grp.add_argument("--paper", action="store_true", help="only paper bets")
            p.add_argument("--series-suffix", help="only series ending in this, e.g. 15M")
            p.add_argument("--days", type=int, default=14, help="how many recent days to list")
            p.add_argument("--tz", default="America/New_York", help="time zone that defines a day")
        if name == "arbs":
            p.add_argument("--top", type=int, default=10, help="how many to list")
        if name in {"prune", "backfill"}:
            p.add_argument("--yes", action="store_true", help="actually delete (a backup is made first)")
        if name == "jobs":
            p.add_argument("action", nargs="?", choices=["retry"], help="requeue dead jobs")
    return parser


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request buries the log
    args = build_parser().parse_args(argv[1:])
    # reload() re-reads .env, so one-off commands see edits made after the container started.
    return COMMANDS[args.command](config.reload(), args)


def cli() -> None:
    """Console-script entry point."""
    raise SystemExit(main(sys.argv))


if __name__ == "__main__":
    cli()
