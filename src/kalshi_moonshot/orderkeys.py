"""The order id of a buy, made before the order is sent and the same after a restart.

2026-11-19 (user: the buy path runs on the websocket and in memory, not through the database job queue). A buy used to take its Kalshi client_order_id from its queued job (the job id), so a job rerun after a crash could not buy twice: Kalshi refuses an id it has seen. With no job there is no id to borrow,
so the id is derived from what is known before the order goes out: the environment, the lane, the market, the side and the number of orders already sent on that market and side.

  - In this process the number is the persisted count at start (`seed`, read by a background job from the orders table) plus the attempts this process has begun (`begin`, called right before the order is sent).
  - After a crash between sending and writing the order row the count is unchanged, so the same attempt gets the same id and Kalshi refuses the duplicate. After the row is written the count has moved on, so a later, deliberate buy on the same market (a re-buy after a sale) gets a new id.
"""

from __future__ import annotations

import threading
import uuid

_lock = threading.Lock()
_seed: dict[tuple[str, str, str], int] = {}  # (env, ticker, side) -> orders rows persisted before this process started
_begun: dict[tuple[str, str, str], int] = {}  # (env, ticker, side) -> attempts this process has sent
_ready = threading.Event()  # set once the persisted counts are loaded: no buy is decided on this path before it (a restart would then repeat an id Kalshi has seen and the order would be refused)


def seed(env: str, counts: dict[tuple[str, str], int]) -> None:
    """Load the persisted order counts, {(ticker, side): n}. Called once at start by a background job; later calls only raise a count."""
    with _lock:
        for (ticker, side), n in counts.items():
            k = (env, ticker, side)
            _seed[k] = max(_seed.get(k, 0), int(n))
    _ready.set()


def ready() -> bool:
    return _ready.is_set()


def index(env: str, ticker: str, side: str) -> int:
    with _lock:
        k = (env, ticker, side)
        return _seed.get(k, 0) + _begun.get(k, 0)


def key(env: str, lane: str, ticker: str, side: str) -> str:
    """The id the next attempt on this market and side will send (the same until `begin` is called)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"moonshot/{env}/{lane}/{ticker}/{side}/{index(env, ticker, side)}"))


def begin(env: str, ticker: str, side: str) -> None:
    """Call right before the order is sent: the next attempt gets a new id, and a restart that finds no order row repeats this one."""
    with _lock:
        k = (env, ticker, side)
        _begun[k] = _begun.get(k, 0) + 1


def reset() -> None:
    with _lock:
        _seed.clear()
        _begun.clear()
    _ready.clear()
