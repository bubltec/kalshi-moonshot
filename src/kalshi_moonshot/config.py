"""All settings come from environment variables (see .env.example).

The hourly review may tighten a few of them in data/tuning.json. That file wins over .env
for those keys only, because the container mounts .env read-only. Delete the file to undo.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

DEMO_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"
PROD_BASE = "https://external-api.kalshi.com/trade-api/v2"


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _csv(name: str, default: str) -> frozenset[str]:
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _sides(name: str) -> tuple[str, ...]:
    """yes, no or both (the default). A long shot is a long shot whichever contract carries it."""
    raw = os.environ.get(name, "yes,no")
    sides = tuple(x for x in ("yes", "no") if x in {p.strip().lower() for p in raw.split(",")})
    if not sides:
        raise ValueError(f"{name} must name yes, no or both")
    return sides


def _live_lanes() -> frozenset[str] | None:
    """LIVE_LANES=commodity,steady names the lanes allowed to trade; "none" allows no lane.

    Unset (or empty) keeps the original meaning of MODE=trade: every lane trades. Naming lanes
    makes every other lane keep paper-trading while the named ones go live.
    """
    raw = os.environ.get("LIVE_LANES", "").strip().lower()
    if not raw:
        return None
    if raw == "none":
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def _b(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- where and how ---
    env: str  # "demo" or "prod"
    mode: str  # "observe" logs decisions only; "trade" may place orders
    allow_real_money: bool  # must be true AND env=prod for real orders
    live_lanes: frozenset[str] | None  # lanes that may place orders in trade mode; None = all of them
    fast_live: bool  # whether 15-minute windows may place real orders (else they paper-trade)
    data_dir: Path

    # --- Kalshi credentials (only needed for balance/trading) ---
    key_id: str
    private_key_path: str

    # --- risk caps (every order passes through risk.py) ---
    max_usd_per_trade: float
    max_loss_per_day: float  # stop buying for the day once realized losses reach this (not a spending cap)
    max_open_positions: int
    min_order_usd: float  # skip a buy when less than this is offered at our price

    # --- long-shot selection ---
    min_price: float  # cheapest ask we will consider, dollars
    max_price: float  # most expensive ask that still counts as a long shot
    min_volume: float  # contracts traded, market lifetime
    max_spread: float  # ask - bid, dollars
    min_hours_to_close: float
    max_hours_to_close: float
    min_edge: float  # model probability must beat ask + fee by this much
    scan_pages: int  # pages of markets, or of events when categories are skipped
    shortlist: int  # markets sent to the model per scan
    scan_interval_minutes: int
    history_enabled: bool  # keep downloading Kalshi's candles for settled 15-minute windows
    history_interval_minutes: int
    history_batch: int  # windows fetched per run, so one run never holds the queue for long
    history_keep_days: int
    account_sync_enabled: bool  # keep a local copy of your fills and settlements (read-only)
    account_sync_minutes: int  # how often
    shard_topup_target: float  # keep this many dollars on the exchange a live order trades on, moving cash in from another one (0 = off)
    shard_topup_keep: float  # never leave the source exchange below this many dollars
    shard_topup_max_per_day: float  # most dollars the bot may move between exchanges per day (New York time)
    patterns_hours: float  # how often to rewrite data/patterns.md from your trades (0 = off)
    tape_enabled: bool  # record contract quotes next to the price they follow (lead-lag study)
    esports_forecast: bool  # forecast CS2 and Dota 2 matches with the local model and news, and score it against the market (esports.py); never trades
    settlement_edge_enabled: bool  # log, as paper bets, the side whose exact settlement chance beats its ask in the last 150 s of every crypto 15-minute market (settlement_edge.py)
    coinbase_feed_enabled: bool  # record Coinbase's real-time spot prices (about one a second per coin) on the tape (coinbasefeed.py)
    fut_flow_enabled: bool  # record Hyperliquid perpetual-futures trade flow per second on the tape (futflow.py)
    fut_flow_coins: tuple[str, ...]
    behavior_cases_hours: float  # how often newly finished windows are added to the case memory (behavior.py); 0 = never
    forecast_refit_hours: float  # how often the forecast weights are refit from the tape while a forecast lane is on (0 = never)
    forecast_weights: str  # JSON file of forecast weights (forecast.py); empty = <data dir>/forecast_h5.json
    watch_series: tuple[str, ...]  # opt-in list of series to follow (tape and discovery); empty = follow every series a lane or the instruments file names. Live lanes' own series are always followed
    tape_extra_series: tuple[str, ...]  # more series (sports, esports...) whose open markets the tape follows, quotes and trade flow
    manual_exit: bool  # sell positions you opened by hand, from the socket, to take a profit (see manual.py); off unless asked for
    manual_exit_skip: tuple[str, ...]  # ticker prefixes the manual exit leaves alone (positions you mean to hold)
    manual_exit_activate: float  # arm once the bid is this fraction over the price paid
    manual_exit_drop: float  # sell when the bid is this many dollars under a peak that held a few seconds
    manual_exit_fast: float  # ... or at once when it falls this far
    manual_exit_min_profit: float  # never sell for less than this fraction over the price paid, after the sell fee
    manual_exit_stop_confirm: float  # seconds the bid must stay past the stop line before a hand-placed position is sold
    manual_exit_unstable_gain: float  # sell a hand-placed position that is up this fraction while the bid is swinging (0 = off)
    manual_exit_unstable_range: float  # ... the bid's high-low in dollars over manual_exit_unstable_seconds
    manual_exit_unstable_seconds: float
    manual_cheap_max: float  # a hand-placed buy at or under this price is a moonshot: hold to settlement (see watcher._cheap_rules)
    manual_cheap_take_x: float  # optional cash-out: sell at once when the bid is this many times the price paid (0 = hold)
    manual_cheap_arm_x: float  # optional trail: arm once the bid has been this many times the price paid (0 = hold)
    manual_cheap_giveback: float  # ... then sell when this share of the gain is given back, never under the price paid
    discovery_enabled: bool  # one thread lists every series the lanes use and fills a shared cache; lanes read it instead of asking Kalshi each (marketdata.py)
    discovery_seconds: float  # how often that thread lists each series again
    discovery_hot_seconds: float  # how often the series the bot is acting on (an open bet, a prospect on a lane's watchlist) are listed again, on a second thread
    discovery_max_age: float  # a lane's scope and review accept cached listings this young (the live quote stream keeps their prices current)
    discovery_eval_age: float  # a PAPER lane's evaluation accepts a cached market this young; a live lane's evaluation always asks Kalshi
    estimate_share_seconds: float  # one lane's estimate of a market (model or ladder price) is reused by every other lane asking within this many seconds; 0 = each lane asks for itself
    flip_after_stop: bool  # after a stop-loss sells a position, ask the model whether the OTHER side is worth buying (paper lane stop_flip; never trades)
    flip_wait_seconds: float  # how long to wait for the model's answer before giving up on a flip
    stop_model_seconds: float  # a stop on a 15-minute market first asks the model whether to hold, waiting at most this long (0 = never ask); a lane opts in with stop_model = true, hand-placed buys with MANUAL_EXIT_STOP_MODEL
    stop_model_recheck_seconds: float  # the model's first "hold" on a stop is soft: the stop looks again after this long, with the price then, and holds on for longer only if the model still says hold
    manual_stop_model: bool  # a hand-placed buy's stop asks the model before selling (same wait; no answer in time = sell)
    fractional_stake: bool  # size a bet to the dollar stake in contracts to the hundredth (a $2 stake buys $2 worth), not the whole contracts that fit under it; falls back to whole contracts if Kalshi refuses a fractional order
    manual_exit_stop: float  # stop-loss on hand-placed positions: sell at this fraction under the price paid (0 = none); same hours rules as the bot's
    tape_dir: Path  # where tape.db lives: a Docker-managed volume in compose, so SQLite's locking is not shared across the Mac/VM boundary
    backup_keep_days: int  # nightly consistent copies of the databases kept in DATA_DIR/backups
    tape_keep_days: float
    archive_bucket: str  # S3 bucket that old tape days and backups are shipped to ('' = no archive; see archive.py)
    archive_region: str
    archive_hot_days: int  # tape days kept in the local database; older ones, once archived and verified, may be deleted locally
    archive_delete_local: bool  # off: the archive only uploads. On: archived days older than archive_hot_days are deleted from the local tape
    max_rps: float  # Kalshi requests per second the bot paces itself to
    page_size: int  # markets and events per Kalshi page (max 1000)
    job_max_attempts: int  # tries per queued job before it is parked as dead
    buy_workers: int  # threads that run only evaluate (buy) jobs, so a scan, review or scope pass never holds a buy back (0 = buys share the main job thread)
    skip_categories: frozenset[str]  # Kalshi event categories, e.g. Sports
    exclude_versions: frozenset[str]  # strategy versions left out of report and similar-case search
    skip_numeric: bool  # skip markets that hinge on a live number the model cannot see
    moonshot_max_drop: float  # skip a long shot whose ask fell this much (live collapse)
    stop_loss_enabled: bool
    stop_loss_hours: float  # only sell when this close to settlement
    crash_giveback: float  # the crash exit sells once this share of the gain (peak over the price paid) is given back; 0 = the fixed trail_drop / manual_exit_drop
    crash_spot_sigma: float  # a crash exit holds while spot is still on the winning side of the strike by more than this many sigma (0 = off)
    crash_progress: float  # the crash exit arms once the bid has covered this share of the way from the price paid to $1.00 (0.40 = 40%); 0 = the lane's trail_activate gain instead
    stop_loss_drop: float  # fraction of entry lost before the stop sells, e.g. 0.50 = 50%
    stop_loss_fast: bool  # whether the stop-loss also covers 15-minute windows (history says holding is better)
    stop_loss_check_minutes: int  # how often to re-check open bets in that window

    # --- steady lane: likely outcomes whose price is trending up, 30-50% gross return ---
    moonshot_sides: tuple[str, ...]  # contracts the moonshot lane may buy: yes, no or both
    kelly_fraction: float  # share of full Kelly to stake; small = slow, steady growth
    paper_bankroll_usd: float  # starting paper balance when not trading with credentials
    moonshot_bet_pct: float  # one bet, share of equity
    moonshot_exposure_pct: float  # all open bets in the lane, share of equity
    steady_bet_pct: float
    steady_exposure_pct: float
    commodity_bet_pct: float
    commodity_exposure_pct: float
    steady_enabled: bool
    steady_min_price: float  # 0.66 nets about +48% after the fee
    steady_max_price: float  # 0.75 nets about +31% after the fee
    steady_max_spread: float
    steady_min_volume: float
    steady_min_hours_to_close: float
    steady_max_hours_to_close: float  # short horizon so it turns over several times a day
    steady_lookback_hours: int  # window the ask must have risen across
    steady_min_trend: float  # dollars the ask must have gained over the window
    steady_min_edge: float
    steady_sides: tuple[str, ...]
    steady_max_lag: float  # how far under the ask the model may sit and still take the favorite
    steady_usd_per_trade: float  # small by design
    steady_shortlist: int

    # --- commodities lane: gold, silver, oil, gas, copper threshold ladders priced by math (commodities.py) ---
    commodities_enabled: bool
    commodity_min_price: float
    commodity_max_price: float
    commodity_min_return: float  # profit as a share of cost if the bet wins, after the fee
    commodity_min_edge: float  # model probability must beat ask + fee by this much
    commodity_max_spread: float
    commodity_min_age_minutes: float  # a market must have been open this long before the lane bets
    commodity_min_volume: float
    commodity_min_hours_to_close: float
    commodity_max_hours_to_close: float
    commodity_vol_mult: float  # scales realized volatility; above 1 widens the tails we price
    commodity_max_basis: float  # skip when the ladder center is this far (fraction) from the feed spot
    commodity_usd_per_trade: float
    commodity_shortlist: int
    commodity_scope_minutes: float  # slow pass: find the ladders worth watching
    commodity_review_minutes: float  # fast pass: re-price only the watched ladders
    commodity_cooldown_minutes: float  # do not re-log a market this soon after its last decision
    commodity_watch_max: int  # events kept on the watchlist, soonest close first
    commodity_fast_from_min: float  # 15-minute markets: earliest to bet, minutes after the window opens
    commodity_fast_to_min: float  # ... and the latest
    commodity_tail_floor: float  # never claim a probability closer than this to 0 or 1 (model risk)
    commodity_basis_unc: float  # width (ln units) added when the center comes from remembered basis
    commodity_realtime_unc: float  # width added when a real-time feed price alone anchors the center
    commodity_window_unc_floor: float  # smallest width allowed for a learned 15-minute offset
    commodity_proxy_unc: float  # width added when a real-time proxy (gold token) uses a measured offset
    commodity_basis_age_hours: float  # how long a remembered feed-to-ladder basis stays usable
    commodity_max_stale_minutes: float  # refuse a feed whose last price is older than this

    # --- external context (news headlines) fed to the model ---
    search_enabled: bool
    search_results: int

    # --- hourly review (see review.py). It can only tighten these. ---
    review_enabled: bool
    review_interval_minutes: int
    review_llm: bool  # after the scorecard pass, send the last hour of bets to the local model for suggested improvements (review.py); never applies them
    rag_skip_lanes: frozenset[str]

    # --- local model (Ollama) ---
    ollama_url: str
    model: str

    @property
    def base_url(self) -> str:
        return PROD_BASE if self.env == "prod" else DEMO_BASE

    def is_live(self, lane: str, series: str = "") -> bool:
        """Whether this lane places real orders: trade mode, and the lane is allowed to.

        15-minute windows (series ending in 15M) additionally need COMMODITY_15M_LIVE, so a new
        short-horizon method can be paper-traded while the lane's slower markets trade for real.
        """
        from .lanes import POLY_LANES

        # Polymarket stays paper unless this lane is named. An empty LIVE_LANES means every Kalshi lane, not these.
        if lane in POLY_LANES and (self.live_lanes is None or lane not in self.live_lanes):
            return False
        if series.endswith(("15M", "5M")) and not self.fast_live:
            return False
        if series and not self._series_may_trade_live(series):
            return False
        return self.mode == "trade" and (self.live_lanes is None or lane in self.live_lanes)

    def _series_may_trade_live(self, series: str) -> bool:
        """False for a series whose instrument is marked `live = false` in instruments.toml (new markets start on paper)."""
        from .commodities import instrument_for, load_instruments

        inst = instrument_for(series, load_instruments(self.data_dir))
        return inst is None or inst.live

    @property
    def real_money(self) -> bool:
        return self.env == "prod" and self.allow_real_money

    @property
    def db_path(self) -> Path:
        return self.data_dir / "moonshot.db"

    @property
    def tape_path(self) -> Path:
        return self.tape_dir / "tape.db"

    @property
    def kill_file(self) -> Path:
        return self.data_dir / "KILL"


def apply_env_file(path: Path) -> bool:
    """Overlay KEY=VALUE lines from a file onto the process environment.

    Returns False when the file is absent. Blank lines and # comments are skipped.
    """
    if not path.is_file():
        return False
    try:
        text = path.read_text()
    except FileNotFoundError:
        # Docker bind-mounts a file by inode; replacing .env on the host leaves a ghost path
        # that still stats as a file but cannot be read until the container is recreated.
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            os.environ[key] = value
    return True


def tuning_path(data_dir: Path) -> Path:
    return data_dir / "tuning.json"


def read_tuning(data_dir: Path) -> dict:
    path = tuning_path(data_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def apply_tuning(s: Settings) -> Settings:
    """Tighten settings from the review file. It cannot turn a lane back on or lower an edge."""
    t = read_tuning(s.data_dir)
    steady = False if t.get("steady_enabled") is False else s.steady_enabled
    skip = set(s.skip_categories)
    skip.update(str(c) for c in (t.get("skip_categories_add") or []) if str(c).strip())
    edge = s.min_edge
    raw_edge = t.get("min_edge")
    if isinstance(raw_edge, (int, float)):
        edge = max(edge, float(raw_edge))
    rag = {str(lane) for lane in (t.get("rag_skip_lanes") or []) if str(lane).strip()}
    return replace(
        s,
        steady_enabled=steady,
        skip_categories=frozenset(skip),
        min_edge=edge,
        rag_skip_lanes=frozenset(rag),
    )


def reload(path: Path | None = None) -> Settings:
    """Re-read the env file, then the settings. Used each scan so .env edits apply."""
    apply_env_file(path or Path(os.environ.get("ENV_FILE", ".env")))
    return load()


def load() -> Settings:
    env = os.environ.get("KALSHI_ENV", "demo").strip().lower()
    if env not in {"demo", "prod"}:
        raise ValueError("KALSHI_ENV must be demo or prod")
    mode = os.environ.get("MODE", "observe").strip().lower()
    if mode not in {"observe", "trade"}:
        raise ValueError("MODE must be observe or trade")
    data_dir = Path(os.environ.get("DATA_DIR", "./data")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    s = Settings(
        env=env,
        mode=mode,
        allow_real_money=_b("ALLOW_REAL_MONEY", False),
        live_lanes=_live_lanes(),
        fast_live=_b("COMMODITY_15M_LIVE", False),
        data_dir=data_dir,
        key_id=os.environ.get("KALSHI_KEY_ID", "").strip(),
        private_key_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip(),
        max_usd_per_trade=_f("MAX_USD_PER_TRADE", 5.0),
        max_loss_per_day=_f("MAX_LOSS_PER_DAY", _f("MAX_USD_PER_DAY", 20.0)),  # the old name still works, now as a loss limit
        max_open_positions=_i("MAX_OPEN_POSITIONS", 8),
        min_order_usd=_f("MIN_ORDER_USD", 0.25),
        min_price=_f("MIN_PRICE", 0.02),
        max_price=_f("MAX_PRICE", 0.10),
        min_volume=_f("MIN_VOLUME", 100.0),
        max_spread=_f("MAX_SPREAD", 0.03),
        min_hours_to_close=_f("MIN_HOURS_TO_CLOSE", 6.0),
        max_hours_to_close=_f("MAX_HOURS_TO_CLOSE", 24.0 * 30),
        min_edge=_f("MIN_EDGE", 0.05),
        scan_pages=_i("SCAN_PAGES", 5),
        shortlist=_i("SHORTLIST", 15),
        scan_interval_minutes=_i("SCAN_INTERVAL_MINUTES", 30),
        history_enabled=_b("HISTORY_ENABLED", True),
        history_interval_minutes=max(5, _i("HISTORY_INTERVAL_MINUTES", 60)),
        history_batch=max(5, _i("HISTORY_BATCH", 30)),
        history_keep_days=max(1, _i("HISTORY_KEEP_DAYS", 30)),
        account_sync_enabled=_b("ACCOUNT_SYNC_ENABLED", True),
        account_sync_minutes=max(5, _i("ACCOUNT_SYNC_MINUTES", 15)),
        patterns_hours=_f("PATTERNS_HOURS", 6.0),
        shard_topup_target=_f("SHARD_TOPUP_TARGET_USD", 0.0),
        shard_topup_keep=_f("SHARD_TOPUP_KEEP_USD", 25.0),
        shard_topup_max_per_day=_f("SHARD_TOPUP_MAX_USD_PER_DAY", 50.0),
        tape_enabled=_b("TAPE_ENABLED", True),
        tape_dir=Path(os.environ.get("TAPE_DIR") or data_dir).expanduser(),
        backup_keep_days=max(1, _i("BACKUP_KEEP_DAYS", 7)),
        tape_keep_days=max(0.5, _f("TAPE_KEEP_DAYS", 3.0)),
        archive_bucket=os.environ.get("ARCHIVE_BUCKET", "").strip(),
        archive_region=os.environ.get("ARCHIVE_REGION", "us-east-1").strip() or "us-east-1",
        archive_hot_days=max(2, _i("ARCHIVE_HOT_DAYS", 14)),
        archive_delete_local=_b("ARCHIVE_DELETE_LOCAL", False),
        esports_forecast=_b("ESPORTS_FORECAST", True),
        settlement_edge_enabled=_b("SETTLEMENT_EDGE_ENABLED", False),  # off: the paper observer's replay edge vanished once fills were delayed (scripts/settlement_edge_fill_study.py)
        coinbase_feed_enabled=_b("COINBASE_FEED_ENABLED", True),
        fut_flow_enabled=_b("FUT_FLOW_ENABLED", True),
        fut_flow_coins=tuple(x.strip().upper() for x in os.environ.get("FUT_FLOW_COINS", "BTC,ETH,SOL,XRP,DOGE").split(",") if x.strip()),
        behavior_cases_hours=max(0.0, _f("BEHAVIOR_CASES_HOURS", 2.0)),
        forecast_refit_hours=max(0.0, _f("FORECAST_REFIT_HOURS", 2.0)),
        forecast_weights=os.environ.get("FORECAST_WEIGHTS", "").strip(),
        watch_series=tuple(x.strip() for x in os.environ.get("WATCH_SERIES", "").split(",") if x.strip()),
        tape_extra_series=tuple(x.strip() for x in os.environ.get("TAPE_EXTRA_SERIES", "").split(",") if x.strip()),
        manual_exit=_b("MANUAL_EXIT", False),
        manual_exit_skip=tuple(x.strip() for x in os.environ.get("MANUAL_EXIT_SKIP", "").split(",") if x.strip()),
        manual_exit_activate=_f("MANUAL_EXIT_ACTIVATE", 0.10),
        manual_exit_drop=_f("MANUAL_EXIT_DROP", 0.04),
        manual_exit_fast=_f("MANUAL_EXIT_FAST", 0.12),
        manual_exit_min_profit=_f("MANUAL_EXIT_MIN_PROFIT", 0.05),
        manual_exit_unstable_gain=_f("MANUAL_EXIT_UNSTABLE_GAIN", 0.0),
        manual_exit_unstable_range=_f("MANUAL_EXIT_UNSTABLE_RANGE", 0.06),
        manual_exit_unstable_seconds=_f("MANUAL_EXIT_UNSTABLE_SECONDS", 20.0),
        manual_cheap_max=_f("MANUAL_CHEAP_MAX", 0.25),
        manual_cheap_take_x=_f("MANUAL_CHEAP_TAKE_X", 0.0),
        manual_cheap_arm_x=_f("MANUAL_CHEAP_ARM_X", 0.0),
        manual_cheap_giveback=_f("MANUAL_CHEAP_GIVEBACK", 0.0),
        discovery_enabled=_b("DISCOVERY_ENABLED", True),
        discovery_seconds=max(5.0, _f("DISCOVERY_SECONDS", 45.0)),
        discovery_hot_seconds=max(5.0, _f("DISCOVERY_HOT_SECONDS", 15.0)),
        discovery_max_age=max(0.0, _f("DISCOVERY_MAX_AGE", 60.0)),
        discovery_eval_age=max(0.0, _f("DISCOVERY_EVAL_AGE", 10.0)),
        estimate_share_seconds=max(0.0, _f("ESTIMATE_SHARE_SECONDS", 60.0)),
        flip_after_stop=_b("FLIP_AFTER_STOP", True),
        flip_wait_seconds=max(30.0, _f("FLIP_WAIT_SECONDS", 240.0)),
        stop_model_seconds=max(0.0, _f("STOP_MODEL_SECONDS", 8.0)),
        stop_model_recheck_seconds=max(1.0, _f("STOP_MODEL_RECHECK_SECONDS", 15.0)),
        manual_stop_model=_b("MANUAL_EXIT_STOP_MODEL", False),
        fractional_stake=_b("FRACTIONAL_STAKE", True),
        manual_exit_stop=_f("MANUAL_EXIT_STOP_LOSS", 0.30),
        manual_exit_stop_confirm=_f("MANUAL_EXIT_STOP_CONFIRM", 20.0),
        skip_categories=_csv("SKIP_CATEGORIES", "Sports"),
        skip_numeric=_b("SKIP_NUMERIC", True),
        moonshot_max_drop=_f("MOONSHOT_MAX_DROP", 0.02),
        stop_loss_enabled=_b("STOP_LOSS_ENABLED", True),
        stop_loss_hours=_f("STOP_LOSS_HOURS", 1.0),
        crash_giveback=_f("CRASH_GIVEBACK", 0.90),
        crash_spot_sigma=_f("CRASH_SPOT_SIGMA", 0.5),
        crash_progress=_f("CRASH_PROGRESS", 0.40),
        stop_loss_drop=_f("STOP_LOSS_DROP", 0.50),
        stop_loss_fast=_b("STOP_LOSS_15M", True),
        stop_loss_check_minutes=max(1, _i("STOP_LOSS_CHECK_MINUTES", 5)),
        max_rps=max(0.5, _f("KALSHI_MAX_RPS", 8.0)),
        page_size=min(1000, max(1, _i("KALSHI_PAGE_SIZE", 1000))),
        job_max_attempts=max(1, _i("JOB_MAX_ATTEMPTS", 3)),
        buy_workers=max(0, _i("BUY_WORKERS", 3)),
        exclude_versions=_csv("EXCLUDE_VERSIONS", "v1"),
        moonshot_sides=_sides("MOONSHOT_SIDES"),
        kelly_fraction=min(1.0, max(0.0, _f("KELLY_FRACTION", 0.25))),
        paper_bankroll_usd=max(1.0, _f("PAPER_BANKROLL_USD", 100.0)),
        moonshot_bet_pct=_f("MOONSHOT_BET_PCT", 0.01),
        moonshot_exposure_pct=_f("MOONSHOT_EXPOSURE_PCT", 0.15),
        steady_bet_pct=_f("STEADY_BET_PCT", 0.03),
        steady_exposure_pct=_f("STEADY_EXPOSURE_PCT", 0.40),
        # The commodity lane's own settings are in data/strategies.toml [lane.commodity]; these are only the built-in fallbacks under it.
        commodity_bet_pct=0.03,
        commodity_exposure_pct=0.40,
        steady_enabled=_b("STEADY_ENABLED", True),
        steady_sides=_sides("STEADY_SIDES"),
        steady_min_price=_f("STEADY_MIN_PRICE", 0.66),
        steady_max_price=_f("STEADY_MAX_PRICE", 0.75),
        steady_max_spread=_f("STEADY_MAX_SPREAD", 0.03),
        steady_min_volume=_f("STEADY_MIN_VOLUME", 200.0),
        steady_min_hours_to_close=_f("STEADY_MIN_HOURS_TO_CLOSE", 1.0),
        steady_max_hours_to_close=_f("STEADY_MAX_HOURS_TO_CLOSE", 72.0),
        steady_lookback_hours=_i("STEADY_LOOKBACK_HOURS", 6),
        steady_min_trend=_f("STEADY_MIN_TREND", 0.02),
        steady_min_edge=_f("STEADY_MIN_EDGE", 0.05),
        steady_max_lag=_f("STEADY_MAX_LAG", 0.15),
        steady_usd_per_trade=_f("STEADY_USD_PER_TRADE", 0.0),
        steady_shortlist=_i("STEADY_SHORTLIST", 10),
        commodities_enabled=True,
        commodity_min_price=0.70,
        commodity_max_price=0.98,
        commodity_min_return=0.10,
        commodity_min_edge=0.015,
        commodity_min_age_minutes=20.0,
        commodity_max_spread=0.02,
        commodity_min_volume=5.0,
        commodity_min_hours_to_close=0.03,
        commodity_max_hours_to_close=2.0,
        commodity_vol_mult=_f("COMMODITY_VOL_MULT", 1.25),
        commodity_max_basis=_f("COMMODITY_MAX_BASIS", 0.04),
        commodity_usd_per_trade=0.0,
        commodity_shortlist=10,
        commodity_scope_minutes=3.0,
        commodity_review_minutes=1.0,
        commodity_cooldown_minutes=15.0,
        commodity_watch_max=40,
        commodity_fast_from_min=8.5,
        commodity_fast_to_min=12.0,
        commodity_tail_floor=min(0.2, max(0.0, _f("COMMODITY_TAIL_FLOOR", 0.005))),
        commodity_basis_unc=max(0.0, _f("COMMODITY_BASIS_UNC", 0.0015)),
        commodity_realtime_unc=max(0.0, _f("COMMODITY_REALTIME_UNC", 0.0004)),
        commodity_proxy_unc=max(0.0, _f("COMMODITY_PROXY_UNC", 0.0010)),
        commodity_window_unc_floor=max(0.0, _f("COMMODITY_WINDOW_UNC_FLOOR", 0.00015)),
        commodity_basis_age_hours=max(0.1, _f("COMMODITY_BASIS_AGE_HOURS", 6.0)),
        commodity_max_stale_minutes=max(1.0, _f("COMMODITY_MAX_STALE_MINUTES", 20.0)),
        search_enabled=_b("SEARCH_ENABLED", True),
        search_results=_i("SEARCH_RESULTS", 5),
        review_enabled=_b("REVIEW_ENABLED", True),
        review_interval_minutes=max(15, _i("REVIEW_INTERVAL_MINUTES", 60)),
        review_llm=_b("REVIEW_LLM", True),
        rag_skip_lanes=frozenset(),
        ollama_url=os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/"),
        model=os.environ.get("MODEL", "qwen3:8b"),
    )
    return apply_tuning(s)
