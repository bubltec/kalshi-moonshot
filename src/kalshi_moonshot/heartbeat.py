"""Tell something outside the host that the bot is working (watchdog, 2026-11-12).

On 2026-11-12 the host lost its network for about 11 hours and nobody was told. Every HEARTBEAT_SECONDS this thread puts one CloudWatch datapoint, KalshiMoonshot/Bot Heartbeat = 1, but only
while the bot is making progress: a job finished or the quote stream delivered an event within STALE_SECONDS. It sends nothing otherwise, and a host with no network cannot send anything. The
alarm in infra/watchdog_stack.py treats missing data as bad, so a dead host, a lost network, a hung worker and a stopped container all end in the same email within about ten minutes.
Idle while HEARTBEAT_ENABLED is off (the default off the cloud host); needs cloudwatch:PutMetricData, granted by the watchdog stack.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("moonshot")

NAMESPACE = "KalshiMoonshot/Bot"
METRIC = "Heartbeat"
HEARTBEAT_SECONDS = 60.0
STALE_SECONDS = 300.0  # no job finished and no stream event for this long = not working


def healthy(last_progress: float, stream_beat: float, now: float | None = None, stale: float = STALE_SECONDS) -> bool:
    now = time.time() if now is None else now
    return now - max(last_progress, stream_beat) <= stale


def beat_once(client: Any, last_progress: float, stream_beat: float, now: float | None = None) -> bool:
    """Put one datapoint when healthy. Returns whether one was sent."""
    if not healthy(last_progress, stream_beat, now):
        log.warning("heartbeat: no progress for over %.0fs; sending nothing so the watchdog fires", STALE_SECONDS)
        return False
    client.put_metric_data(Namespace=NAMESPACE, MetricData=[{"MetricName": METRIC, "Value": 1.0, "Unit": "Count"}])
    return True


def start(get_enabled: Callable[[], bool], region: str, progress: Callable[[], tuple[float, float]]) -> None:
    """progress() returns (time of the last finished job, time of the last stream event)."""

    def loop() -> None:
        client: Any = None
        time.sleep(30.0)
        while True:
            if get_enabled():
                try:
                    if client is None:
                        import boto3

                        client = boto3.client("cloudwatch", region_name=region)
                    beat_once(client, *progress())
                except Exception:
                    log.exception("heartbeat: could not send")  # no network is the case this exists for; the watchdog sees the gap
            time.sleep(HEARTBEAT_SECONDS)

    threading.Thread(target=loop, name="heartbeat", daemon=True).start()
