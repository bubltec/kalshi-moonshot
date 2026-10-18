"""All settings come from environment variables (see .env.example). One place, no JSON files."""

from __future__ import annotations

import os
from dataclasses import dataclass
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
    job_max_attempts: int  # tries per queued job before it is parked as dead
    skip_categories: frozenset[str]  # Kalshi event categories, e.g. Sports
    exclude_versions: frozenset[str]  # strategy versions left out of report and similar-case search
    skip_numeric: bool  # skip markets that hinge on a live number the model cannot see

    # --- steady lane: likely outcomes whose price is trending up, 30-50% gross return ---
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
    steady_usd_per_trade: float  # small by design
    steady_shortlist: int

    # --- external context (news headlines) fed to the model ---
    search_enabled: bool
    search_results: int

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
    return Settings(
        env=env,
        mode=mode,
        allow_real_money=_b("ALLOW_REAL_MONEY", False),
        data_dir=data_dir,
        key_id=os.environ.get("KALSHI_KEY_ID", "").strip(),
        private_key_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip(),
        max_usd_per_trade=_f("MAX_USD_PER_TRADE", 5.0),
        max_usd_per_day=_f("MAX_USD_PER_DAY", 20.0),
        max_open_positions=_i("MAX_OPEN_POSITIONS", 8),
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
        max_rps=max(0.5, _f("KALSHI_MAX_RPS", 8.0)),
        job_max_attempts=max(1, _i("JOB_MAX_ATTEMPTS", 3)),
        exclude_versions=_csv("EXCLUDE_VERSIONS", "v1"),
        steady_enabled=_b("STEADY_ENABLED", True),
        steady_min_price=_f("STEADY_MIN_PRICE", 0.66),
        steady_max_price=_f("STEADY_MAX_PRICE", 0.75),
        steady_max_spread=_f("STEADY_MAX_SPREAD", 0.03),
        steady_min_volume=_f("STEADY_MIN_VOLUME", 200.0),
        steady_min_hours_to_close=_f("STEADY_MIN_HOURS_TO_CLOSE", 1.0),
        steady_max_hours_to_close=_f("STEADY_MAX_HOURS_TO_CLOSE", 72.0),
        steady_lookback_hours=_i("STEADY_LOOKBACK_HOURS", 6),
        steady_min_trend=_f("STEADY_MIN_TREND", 0.02),
        steady_min_edge=_f("STEADY_MIN_EDGE", 0.05),
        steady_usd_per_trade=_f("STEADY_USD_PER_TRADE", 2.0),
        steady_shortlist=_i("STEADY_SHORTLIST", 10),
        search_enabled=_b("SEARCH_ENABLED", True),
        search_results=_i("SEARCH_RESULTS", 5),
        ollama_url=os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/"),
        model=os.environ.get("MODEL", "qwen3:8b"),
    )
