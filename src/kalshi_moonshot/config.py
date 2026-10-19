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
    data_dir: Path

    # --- Kalshi credentials (only needed for balance/trading) ---
    key_id: str
    private_key_path: str

    # --- risk caps (every order passes through risk.py) ---
    max_usd_per_trade: float
    max_usd_per_day: float
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
    max_rps: float  # Kalshi requests per second the bot paces itself to
    page_size: int  # markets and events per Kalshi page (max 1000)
    job_max_attempts: int  # tries per queued job before it is parked as dead
    skip_categories: frozenset[str]  # Kalshi event categories, e.g. Sports
    exclude_versions: frozenset[str]  # strategy versions left out of report and similar-case search
    skip_numeric: bool  # skip markets that hinge on a live number the model cannot see
    moonshot_max_drop: float  # skip a long shot whose ask fell this much (live collapse)
    stop_loss_enabled: bool
    stop_loss_hours: float  # only sell when this close to settlement
    stop_loss_drop: float  # fraction of entry lost, e.g. 0.30 = 30%
    stop_loss_check_minutes: int  # how often to re-check open bets in that window

    # --- steady lane: likely outcomes whose price is trending up, 30-50% gross return ---
    moonshot_sides: tuple[str, ...]  # contracts the moonshot lane may buy: yes, no or both
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
    commodity_min_edge: float  # model probability must beat ask + fee by this much
    commodity_max_spread: float
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
    commodity_tail_floor: float  # never claim a probability closer than this to 0 or 1 (model risk)
    commodity_basis_unc: float  # width (ln units) added when the center comes from remembered basis
    commodity_basis_age_hours: float  # how long a remembered feed-to-ladder basis stays usable
    commodity_max_stale_minutes: float  # refuse a feed whose last price is older than this

    # --- external context (news headlines) fed to the model ---
    search_enabled: bool
    search_results: int

    # --- hourly review (see review.py). It can only tighten these. ---
    review_enabled: bool
    review_interval_minutes: int
    rag_skip_lanes: frozenset[str]

    # --- local model (Ollama) ---
    ollama_url: str
    model: str

    @property
    def base_url(self) -> str:
        return PROD_BASE if self.env == "prod" else DEMO_BASE

    @property
    def real_money(self) -> bool:
        return self.env == "prod" and self.allow_real_money

    @property
    def db_path(self) -> Path:
        return self.data_dir / "moonshot.db"

    @property
    def kill_file(self) -> Path:
        return self.data_dir / "KILL"


def apply_env_file(path: Path) -> bool:
    """Overlay KEY=VALUE lines from a file onto the process environment.

    Returns False when the file is absent. Blank lines and # comments are skipped.
    """
    if not path.is_file():
        return False
    for raw in path.read_text().splitlines():
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
        data_dir=data_dir,
        key_id=os.environ.get("KALSHI_KEY_ID", "").strip(),
        private_key_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip(),
        max_usd_per_trade=_f("MAX_USD_PER_TRADE", 5.0),
        max_usd_per_day=_f("MAX_USD_PER_DAY", 20.0),
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
        skip_categories=_csv("SKIP_CATEGORIES", "Sports"),
        skip_numeric=_b("SKIP_NUMERIC", True),
        moonshot_max_drop=_f("MOONSHOT_MAX_DROP", 0.02),
        stop_loss_enabled=_b("STOP_LOSS_ENABLED", True),
        stop_loss_hours=_f("STOP_LOSS_HOURS", 1.0),
        stop_loss_drop=_f("STOP_LOSS_DROP", 0.30),
        stop_loss_check_minutes=max(1, _i("STOP_LOSS_CHECK_MINUTES", 5)),
        max_rps=max(0.5, _f("KALSHI_MAX_RPS", 8.0)),
        page_size=min(1000, max(1, _i("KALSHI_PAGE_SIZE", 1000))),
        job_max_attempts=max(1, _i("JOB_MAX_ATTEMPTS", 3)),
        exclude_versions=_csv("EXCLUDE_VERSIONS", "v1"),
        moonshot_sides=_sides("MOONSHOT_SIDES"),
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
        steady_usd_per_trade=_f("STEADY_USD_PER_TRADE", 2.0),
        steady_shortlist=_i("STEADY_SHORTLIST", 10),
        commodities_enabled=_b("COMMODITIES_ENABLED", True),
        commodity_min_price=_f("COMMODITY_MIN_PRICE", 0.70),
        commodity_max_price=_f("COMMODITY_MAX_PRICE", 0.98),
        commodity_min_edge=_f("COMMODITY_MIN_EDGE", 0.015),
        commodity_max_spread=_f("COMMODITY_MAX_SPREAD", 0.04),
        commodity_min_volume=_f("COMMODITY_MIN_VOLUME", 5.0),
        commodity_min_hours_to_close=_f("COMMODITY_MIN_HOURS_TO_CLOSE", 0.03),
        commodity_max_hours_to_close=_f("COMMODITY_MAX_HOURS_TO_CLOSE", 24.0),
        commodity_vol_mult=_f("COMMODITY_VOL_MULT", 1.25),
        commodity_max_basis=_f("COMMODITY_MAX_BASIS", 0.04),
        commodity_usd_per_trade=_f("COMMODITY_USD_PER_TRADE", 2.0),
        commodity_shortlist=_i("COMMODITY_SHORTLIST", 10),
        commodity_scope_minutes=max(1.0, _f("COMMODITY_SCOPE_MINUTES", 10.0)),
        commodity_review_minutes=max(0.5, _f("COMMODITY_REVIEW_MINUTES", 2.0)),
        commodity_cooldown_minutes=max(0.0, _f("COMMODITY_COOLDOWN_MINUTES", 15.0)),
        commodity_watch_max=max(1, _i("COMMODITY_WATCH_MAX", 40)),
        commodity_tail_floor=min(0.2, max(0.0, _f("COMMODITY_TAIL_FLOOR", 0.005))),
        commodity_basis_unc=max(0.0, _f("COMMODITY_BASIS_UNC", 0.0015)),
        commodity_basis_age_hours=max(0.1, _f("COMMODITY_BASIS_AGE_HOURS", 6.0)),
        commodity_max_stale_minutes=max(1.0, _f("COMMODITY_MAX_STALE_MINUTES", 20.0)),
        search_enabled=_b("SEARCH_ENABLED", True),
        search_results=_i("SEARCH_RESULTS", 5),
        review_enabled=_b("REVIEW_ENABLED", True),
        review_interval_minutes=max(15, _i("REVIEW_INTERVAL_MINUTES", 60)),
        rag_skip_lanes=frozenset(),
        ollama_url=os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/"),
        model=os.environ.get("MODEL", "qwen3:8b"),
    )
    return apply_tuning(s)
