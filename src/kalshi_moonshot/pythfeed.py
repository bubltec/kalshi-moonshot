"""Pyth prices for the commodity markets, from the Pyth Core (Hermes) stream. Kalshi settles these 15-minute markets on Pyth, and its strike is the Pyth value at the open, so a Pyth tick and the strike are on the same scale.

Needs an API key (PYTH_API_KEY, or PYTH_API_KEY_PATH naming a file that holds it). Without one nothing starts. Each feed becomes a tape index named in FEEDS, one tick a second like the crypto indexes, so the tape can replay a strike-through
and the watcher can run it. Read-only; places nothing.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("pythfeed")

DEFAULT_URL = "https://pyth.dourolabs.app/hermes"

# tape index id -> (Kalshi series prefix, Pyth feed id). The feed is the one named in each series' settlement source.
FEEDS: dict[str, tuple[str, str]] = {
    "PYTH_GOLD": ("KXGOLD", "7fd2c87083dd8fd5af2486a43f40b8e444acd77fda4e96263a53421cd079c387"),
    "PYTH_SILVER": ("KXSILVER", "6afca47e1c79ecd0d2844327efb463478417a9d498dfb0915ce5e88ae6d952d3"),
    "PYTH_WTI": ("KXWTI", "67784f72e95ac01337edb7d7bd5bbd1c03669101b7068a620df228ed4e52ef14"),
    "PYTH_BRENT": ("KXBRENT", "f33ce961935076ef4dc98be75cf2126046eac1bffdcd7a0fa05ccf18b746fda6"),
    "PYTH_NATGAS": ("KXNATGAS", "45f95717fbe158e896415d1cea33814d73e54169022a8f7bcbb815ebef92f71c"),
    "PYTH_COPPER": ("KXCOPPER", "b2b238aeb6ef5a722c5cf278595bf40434e174cac4f88c8ad5b6f5009b548c59"),
    "PYTH_PLATINUM": ("KXPLATINUM", "398e4bbc7cbf89d6648c21e08019d878967677753b3096799595c78f805a34e5"),
    "PYTH_PALLADIUM": ("KXPALLADIUM", "80367e9664197f37d89a07a804dffd2101c479c7c4e8490501bc9d9e1e7f9021"),
}
INDEX_FOR_PREFIX = {prefix: index for index, (prefix, _) in FEEDS.items()}
_BY_FEED = {fid: index for index, (_, fid) in FEEDS.items()}


def index_for_series(series: str) -> str | None:
    """The Pyth tape index that settles a commodity 15-minute series, or None."""
    for prefix, index in INDEX_FOR_PREFIX.items():
        if series.startswith(prefix) and series[len(prefix):] in {"", "D", "15M"}:
            return index
    return None


def api_key() -> str:
    key = os.environ.get("PYTH_API_KEY", "").strip()
    path = os.environ.get("PYTH_API_KEY_PATH", "").strip()
    if not key and path:
        try:
            key = Path(path).read_text().strip()
        except OSError:
            log.warning("pyth: cannot read PYTH_API_KEY_PATH %s", path)
    return key


def parse_update(frame: dict[str, Any], received: float) -> list[dict[str, Any]]:
    """The index events in one stream message: one per known feed that carries a price."""
    out: list[dict[str, Any]] = []
    for item in frame.get("parsed") or []:
        index = _BY_FEED.get(str(item.get("id", "")).removeprefix("0x"))
        price = item.get("price") or {}
        try:
            value = int(price["price"]) * 10.0 ** int(price["expo"])
            published = float(price["publish_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if index is None or value <= 0:
            continue
        out.append({"kind": "index", "ts": received, "index_id": index, "value": value, "avg60": None, "quarter_avg": None, "src_ms": published * 1000.0, "sts": None})
    return out


class PythFeed:
    def __init__(self, on_event: Callable[[dict[str, Any]], None], key: str, url: str = "", indices: list[str] | None = None):
        self.on_event, self.key = on_event, key
        self.url = (url or os.environ.get("PYTH_URL", "") or DEFAULT_URL).rstrip("/")
        self.ids = [FEEDS[i][1] for i in (indices or list(FEEDS))]
        self._sent: dict[str, int] = {}  # index id -> the last second emitted: one tick a second, like the crypto indexes
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_event_at: float | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pyth-stream", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()

    def handle(self, raw: str, received: float | None = None) -> None:
        now = time.time() if received is None else received
        try:
            frame = json.loads(raw)
        except ValueError:
            return
        for event in parse_update(frame, now):
            second = int(event["ts"])
            if self._sent.get(event["index_id"]) == second:
                continue
            self._sent[event["index_id"]] = second
            self.last_event_at = now
            self.on_event(event)

    def _run(self) -> None:
        backoff = 1.0
        params: list[tuple[str, str | int | float | bool | None]] = [("ids[]", i) for i in self.ids]
        params += [("parsed", "true"), ("allow_unordered", "true"), ("encoding", "hex")]
        headers = {"Authorization": f"Bearer {self.key}", "Accept": "text/event-stream"}
        while not self._stop.is_set():
            try:
                with httpx.Client(timeout=httpx.Timeout(10.0, read=30.0)) as http, http.stream("GET", f"{self.url}/v2/updates/price/stream", params=params, headers=headers) as resp:
                    if resp.status_code != 200:
                        log.warning("pyth stream: HTTP %s; retrying in %.0fs", resp.status_code, backoff)
                        raise httpx.HTTPError(f"status {resp.status_code}")
                    backoff = 1.0
                    log.info("pyth stream connected: %d feeds", len(self.ids))
                    for line in resp.iter_lines():
                        if self._stop.is_set():
                            return
                        if line.startswith("data:"):
                            self.handle(line[5:].strip())
            except Exception as exc:
                log.warning("pyth stream: %s; reconnecting in %.0fs", exc, backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)
