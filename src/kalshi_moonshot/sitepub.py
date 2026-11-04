"""Keep the public website's numbers fresh: every SITE_PUBLISH_MINUTES the bot rebuilds public.json (counts only, current lanes, a day behind: exporter.py) and uploads it.

It writes one object, public/public.json, in the bucket the KalshiMoonshotSite stack made, with the host's IAM role (infra/bot_stack.py grants exactly that key). admin.json is never
built here. The file is checked against the exporter's allowlist before it leaves, and a failed pass only logs: the page keeps the last file. Idle while SITE_BUCKET is empty.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from . import control, exporter, lanes, overrides
from .config import Settings

log = logging.getLogger("moonshot")

KEY = "public/public.json"
CACHE_CONTROL = "public, max-age=60"
ADMIN_KEY = "admin/admin.json"  # not under public/, so the public site cannot reach it; the admin site serves it only to a signed-in session


def client_for(s: Settings) -> Any:
    import boto3

    return boto3.client("s3", region_name=s.site_region)


def publish_once(s: Settings, store: Any, client: Any, now: float | None = None) -> int:
    """Build, check and upload the file. Returns its size in bytes."""
    data = exporter.public_json(store, now, active=exporter.public_lane_names(s))
    body = json.dumps(data, indent=1).encode()
    client.put_object(Bucket=s.site_bucket, Key=KEY, Body=body, ContentType="application/json", CacheControl=CACHE_CONTROL)
    return len(body)


def publish_admin_once(s: Settings, store: Any, client: Any, now: float | None = None) -> int:
    """The admin file (dollars) goes to its own private key, never the public one."""
    specs = lanes.lane_specs(s)
    data = exporter.admin_json(store, now, active=set(specs))
    path = lanes.strategies_path(s.data_dir)
    base = overrides.effective(lanes.lane_specs_from(s, path.read_text() if path.is_file() else None, None))  # the lane file alone, so the page can mark what was changed on it
    data["settings"] = {"schema": overrides.schema(), "lanes": overrides.effective(specs), "base": base}  # what the editor shows; the bounds are overrides.TUNABLES
    try:
        data["control"] = control.state(s, client)
    except Exception:
        log.exception("site: could not read the control state")
    body = json.dumps(data, indent=1).encode()
    client.put_object(Bucket=s.site_bucket, Key=ADMIN_KEY, Body=body, ContentType="application/json", CacheControl="private, no-store")
    return len(body)


def start(get_settings: Callable[[], Settings], make_store: Callable[[], Any]) -> None:
    def loop() -> None:
        time.sleep(300.0)  # let the bot settle after a start
        while True:
            s = get_settings()
            if s.site_bucket:
                try:
                    size = publish_once(s, make_store(), client_for(s))
                    log.info("site: published %s (%d bytes)", KEY, size)
                except Exception:
                    log.exception("site: publish failed; the page keeps the last file")
                try:
                    log.info("site: published %s (%d bytes)", ADMIN_KEY, publish_admin_once(s, make_store(), client_for(s)))
                except Exception:
                    log.exception("site: admin publish failed; the admin page keeps the last file")
            time.sleep(max(300.0, s.site_publish_minutes * 60.0))

    threading.Thread(target=loop, name="sitepub", daemon=True).start()
