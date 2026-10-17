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
    scan_pages: int  # pages of 200 markets per scan
    shortlist: int  # markets sent to the model per scan
    scan_interval_minutes: int

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
        ollama_url=os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/"),
        model=os.environ.get("MODEL", "qwen3:8b"),
    )
