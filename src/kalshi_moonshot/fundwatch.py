"""Keep the cash on every exchange shard a live lane trades on, and say so when it cannot (user, 2026-11-21: "it should constantly make sure whatever exchanges are live and betting have enough cash in them to bet. And notify me if not").

Kalshi keeps a cash balance for each exchange shard a market trades on, and an order can only spend the balance of its own shard. Before this job the shards were filled in two places only: once at start-up for shards that already held a position, and at the moment of an order (the order's own shard, a funds check made as part of sending it). A shard that ran low between orders, or one a quiet lane would trade on after the weekend, was found out only when an order was refused.

Every INTERVAL seconds, in the background (a maintenance job, never on a tick): find the shard of each series a live lane trades on, read the cash per shard (the same one balance call the account snapshot makes), and top up any shard under SHARD_TOPUP_TARGET_USD from the richest other shard (`runner._fill_shard`: the same rules, the source never below SHARD_TOPUP_KEEP_USD, the daily cap). A shard still short after that, or an account whose total cash cannot cover every live shard, is a problem: it is logged as `FUNDS ALERT` (repeated at most every REPEAT seconds), and the metric KalshiMoonshot/Bot ExchangeShortfall is put (the number of problems; 0 when all is well) so the watchdog alarm in infra/watchdog_stack.py sends the email. The metric is sent only where HEARTBEAT_ENABLED is on (the cloud host).

It moves cash between the account's own shards, as the order-time top-up already did, and places no order. Idle unless trading with SHARD_TOPUP_TARGET_USD above 0 and an authenticated client. Stdlib only (boto3 only through the same path the heartbeat uses).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("moonshot")

INTERVAL = 60.0
REPEAT = 1800.0  # the same problem is logged again no sooner than this
NAMESPACE = "KalshiMoonshot/Bot"
METRIC = "ExchangeShortfall"

_SHARD_OF: dict[str, int] = {}  # series -> exchange_index, learned once from a market of the series
_LAST_ALERT: dict[str, float] = {}


def live_series(s: Any, specs: dict[str, Any]) -> list[tuple[str, str]]:
    """(lane, series) for every series a live lane is limited to. A lane with no series list is not here: it cannot be mapped to a shard ahead of an order, and the order-time top-up still covers it."""
    out: list[tuple[str, str]] = []
    for lane, spec in specs.items():
        for series in getattr(spec, "series", ()) or ():
            if s.is_live(lane, series):
                out.append((lane, series))
    return out


def shard_of(client: Any, series: str) -> int | None:
    """The exchange shard a series trades on: the bot's own memory of it, else asked once from one open market of the series."""
    from . import runner

    known = runner._EXCHANGE_OF.get(series)
    if known is not None:
        return int(known)
    if series in _SHARD_OF:
        return _SHARD_OF[series]
    markets = client.series_markets(series)
    for m in markets or []:
        raw = m.get("exchange_index")
        if raw is None and m.get("ticker"):
            raw = client.market(m["ticker"]).get("exchange_index")
        if raw is not None:
            _SHARD_OF[series] = int(raw)
            return int(raw)
    return None


def check_once(s: Any, client: Any, store: Any, specs: dict[str, Any]) -> list[str]:
    """One pass. Returns the problems (empty when every live shard holds its target). Tops up what it can."""
    from . import runner
    from .kalshi import KalshiError, KalshiRateLimited

    target = s.shard_topup_target
    if target <= 0 or s.mode != "trade" or not getattr(client, "authed", False):
        return []
    shards: dict[int, list[str]] = {}
    for _lane, series in live_series(s, specs):
        try:
            idx = shard_of(client, series)
        except KalshiRateLimited:
            raise
        except KalshiError as exc:
            log.warning("fund watch: could not read the exchange of %s: %s", series, exc)
            continue
        if idx is not None and series not in shards.setdefault(idx, []):
            shards[idx].append(series)
    if not shards:
        return []
    problems: list[str] = []
    cash = client.shard_balances()
    for idx in sorted(shards):
        have = cash.get(idx, 0.0)
        if have + 1e-9 >= target:
            continue
        try:
            note = runner._fill_shard(s, client, store, idx, target, "fund watch: a live exchange under its target")
        except KalshiRateLimited:
            raise
        except KalshiError as exc:
            note = f"the top-up failed ({exc})"
        if note:
            problems.append(f"exchange {idx} ({', '.join(sorted(shards[idx]))}) holds ${have:.2f}, under the ${target:.2f} a live lane needs: {note}")
    total = sum(cash.values())
    if total + 1e-9 < target * len(shards):
        problems.append(f"the account holds ${total:.2f} in all, less than the ${target * len(shards):.2f} that {len(shards)} live exchange(s) need")
    return problems


def report(problems: list[str], now: float | None = None, put_metric: Callable[[float], None] | None = None) -> list[str]:
    """Log each problem (at most once every REPEAT seconds) and put the metric. Returns the lines logged."""
    now = time.time() if now is None else now
    logged: list[str] = []
    for p in problems:
        if p not in _LAST_ALERT or now - _LAST_ALERT[p] >= REPEAT:
            _LAST_ALERT[p] = now
            log.warning("FUNDS ALERT: %s", p)
            logged.append(p)
    for k in [k for k in _LAST_ALERT if k not in problems]:
        _LAST_ALERT.pop(k)  # a problem that cleared is announced again if it returns
    if put_metric is not None:
        put_metric(float(len(problems)))
    return logged


def start(get_settings: Callable[[], Any], get_client: Callable[[], Any], get_store: Callable[[], Any], get_specs: Callable[[], dict[str, Any]]) -> None:
    def loop() -> None:
        cw: Any = None
        time.sleep(45.0)
        while True:
            try:
                s = get_settings()
                put: Callable[[float], None] | None = None
                if s.heartbeat_enabled:
                    if cw is None:
                        import boto3

                        cw = boto3.client("cloudwatch", region_name=s.heartbeat_region)

                    def put(v: float, _cw: Any = cw) -> None:
                        _cw.put_metric_data(Namespace=NAMESPACE, MetricData=[{"MetricName": METRIC, "Value": v, "Unit": "Count"}])

                if s.mode == "trade" and s.shard_topup_target > 0:
                    report(check_once(s, get_client(), get_store(), get_specs()), put_metric=put)
            except Exception:
                log.exception("fund watch: a pass failed; the next one retries")
            time.sleep(INTERVAL)

    threading.Thread(target=loop, name="fund-watch", daemon=True).start()
