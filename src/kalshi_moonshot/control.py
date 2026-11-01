"""Pull the admin page's lane changes into the bot.

The admin page's Lambda writes control/overrides.json in the site bucket. Every CONTROL_SECONDS the bot reads it; when its revision differs from the file it last applied, it checks the
document against overrides.py (the real limits) and against the lane rules in lanes.py, then writes DATA_DIR/overrides.json, which lanes.lane_specs() picks up on the next scan. Either way it
writes control/status.json so the page can show whether its change landed or why it did not. Nothing reaches the bot from outside: it only reads one object and writes one. Idle while
SITE_BUCKET is empty.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from . import lanes, overrides
from .config import Settings

log = logging.getLogger("moonshot")

KEY = "control/overrides.json"
STATUS_KEY = "control/status.json"
CONTROL_SECONDS = 60.0


def _read(client: Any, bucket: str, key: str) -> str | None:
    try:
        return client.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    except Exception as exc:  # a missing object is the normal first state
        if getattr(exc, "response", {}).get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def _write_status(s: Settings, client: Any, revision: Any, state: str, errors: list[str], now: float) -> None:
    body = {"revision": revision, "state": state, "errors": errors, "at": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds")}
    client.put_object(Bucket=s.site_bucket, Key=STATUS_KEY, Body=json.dumps(body).encode(), ContentType="application/json", CacheControl="private, no-store")


def sync_once(s: Settings, client: Any, now: float | None = None) -> bool:
    """One pull. Returns True when the status changed (a change was applied or refused), so the caller can republish admin.json."""
    now = time.time() if now is None else now
    text = _read(client, s.site_bucket, KEY)
    if text is None:
        return False
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    revision = doc.get("revision") if isinstance(doc, dict) else None
    local = overrides.parse(overrides.load_text(s.data_dir))
    if local is not None and doc is not None and local.get("revision") == revision:
        return False
    # A rejected revision is not tried again until the page writes a new one: its status already says why.
    last = _read(client, s.site_bucket, STATUS_KEY)
    if last and doc is not None:
        try:
            seen = json.loads(last)
        except ValueError:
            seen = {}
        if seen.get("revision") == revision and seen.get("state") == "rejected":
            return False

    names = set(lanes.lane_specs(s))
    errors = overrides.validate(doc, names) if doc is not None else ["control/overrides.json is not JSON"]
    if not errors:
        try:  # the lane rules in lanes.py (an exit that needs another field set, for one) can still refuse a combination
            lanes.lane_specs_from(s, _strategies_text(s), text)
        except ValueError as exc:
            errors = [str(exc)]
    if errors:
        log.warning("control: revision %s refused: %s", revision, "; ".join(errors))
        _write_status(s, client, revision, "rejected", errors, now)
        return True
    path = s.data_dir / overrides.FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)  # the scan never sees half a file
    log.info("control: applied revision %s (%d lane(s) changed)", revision, len(doc["lanes"]))
    _write_status(s, client, revision, "applied", [], now)
    return True


def state(s: Settings, client: Any) -> dict[str, Any]:
    """For admin.json: the revision the bot runs, the page's last change and what became of it, and the recent history the page's Lambda keeps in the document."""
    out: dict[str, Any] = {"applied_revision": (overrides.parse(overrides.load_text(s.data_dir)) or {}).get("revision")}
    for name, key in (("status", STATUS_KEY), ("document", KEY)):
        try:
            text = _read(client, s.site_bucket, key)
            doc = json.loads(text) if text else None
        except Exception:
            doc = None
        out[name] = doc if name == "status" else ({"revision": doc.get("revision"), "history": doc.get("history", [])[-50:]} if isinstance(doc, dict) else None)
    return out


def _strategies_text(s: Settings) -> str | None:
    path = lanes.strategies_path(s.data_dir)
    return path.read_text() if path.is_file() else None


def start(get_settings: Callable[[], Settings], on_change: Callable[[Settings], None]) -> None:
    from . import sitepub

    def loop() -> None:
        time.sleep(120.0)
        while True:
            s = get_settings()
            if s.site_bucket:
                try:
                    if sync_once(s, sitepub.client_for(s)):
                        on_change(s)
                except Exception:
                    log.exception("control: sync failed; the lanes keep their current settings")
            time.sleep(CONTROL_SECONDS)

    threading.Thread(target=loop, name="control", daemon=True).start()
