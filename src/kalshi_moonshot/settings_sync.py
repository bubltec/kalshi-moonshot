"""Keep the lane file and the admin page's changes from drifting apart.

A lane's settings come from two places: data/strategies.toml (pushed with scripts/cloud_push.sh) and the changes made on the admin page's Settings tab (control/overrides.json in the site bucket,
applied by the bot on top of the file). A change on the page wins over the file, so a value edited in the file can silently stop mattering. This lists every page change next to the file's value and
settles them one of two ways:

    python -m kalshi_moonshot.settings_sync status    # page changes beside the file's values; exit 1 when any exist
    python -m kalshi_moonshot.settings_sync fold      # write the page's values into strategies.toml, then clear the page's changes (push the file after)
    python -m kalshi_moonshot.settings_sync drop      # throw the page's changes away; the file stays as it is

Needs SITE_BUCKET (the site bucket's name) and AWS credentials that can write control/ there (AWS_PROFILE). Nothing is ever sent to the exchange from here.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config, lanes, overrides

KEY = "control/overrides.json"


def differences(s: config.Settings, toml_text: str | None, doc: dict[str, Any] | None) -> list[tuple[str, str, Any, Any]]:
    """(lane, field, the file's value, the page's value) for every page change that differs from what the file alone gives."""
    base = overrides.effective(lanes.lane_specs_from(s, toml_text, None))
    out = []
    for lane, fields in ((doc or {}).get("lanes") or {}).items():
        for field, value in fields.items():
            file_value = base.get(lane, {}).get(field)
            if file_value != value:
                out.append((lane, field, file_value, value))
    return out


def toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value)


def set_in_toml(text: str, lane: str, field: str, value: Any) -> str:
    """Set `field` in the [lane.<lane>] section, keeping the line's trailing comment. A field the section does not have is added at the end of the section."""
    lines = text.splitlines(keepends=True)
    header = f"[lane.{lane}]"
    start = next((i for i, ln in enumerate(lines) if ln.strip() == header), None)
    if start is None:
        raise ValueError(f"no {header} section in strategies.toml (a built-in lane): add the section first")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    pat = re.compile(rf"^(\s*{re.escape(field)}\s*=\s*)([^#\n]*?)(\s*(?:#.*)?)$")
    for i in range(start + 1, end):
        m = pat.match(lines[i].rstrip("\n"))
        if m:
            tail = m.group(3) if m.group(3).strip() else ""
            lines[i] = f"{m.group(1)}{toml_literal(value)}{('  ' + tail.strip()) if tail else ''}\n"
            return "".join(lines)
    last = end - 1
    while last > start and not lines[last].strip():
        last -= 1
    lines.insert(last + 1, f"{field} = {toml_literal(value)}\n")
    return "".join(lines)


def _client() -> Any:
    import os

    import boto3

    # .env carries the bot's own narrow keys; AWS_PROFILE is the person at the keyboard and wins here
    session = boto3.Session(profile_name=os.environ["AWS_PROFILE"]) if os.environ.get("AWS_PROFILE") else boto3.Session()
    return session.client("s3", region_name="us-east-1")


def _read(client: Any, bucket: str) -> dict[str, Any] | None:
    try:
        return json.loads(client.get_object(Bucket=bucket, Key=KEY)["Body"].read())
    except client.exceptions.NoSuchKey:
        return None


def _clear(client: Any, bucket: str, doc: dict[str, Any], note: str, changes: dict[str, Any]) -> int:
    """A new revision with no lane changes, so the bot drops its copy within a minute; the history and an audit record say what happened and why."""
    revision = int(doc.get("revision") or 0) + 1
    at = datetime.now(UTC).isoformat(timespec="seconds")
    entry = {"revision": revision, "at": at, "by": "sync", "lane": "*", "note": note, "changes": changes}
    new = {"revision": revision, "updated": at, "by": "sync", "lanes": {}, "history": [*(doc.get("history") or []), entry][-200:]}
    put = {"ContentType": "application/json", "CacheControl": "private, no-store"}
    client.put_object(Bucket=bucket, Key=f"control/audit/{revision:06d}.json", Body=json.dumps(entry).encode(), **put)
    client.put_object(Bucket=bucket, Key=KEY, Body=json.dumps(new).encode(), **put)
    return revision


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("status", "fold", "drop"))
    ap.add_argument("--file", default="data/strategies.toml")
    args = ap.parse_args(argv)
    s = config.reload()
    if not s.site_bucket:
        print("SITE_BUCKET is not set", file=sys.stderr)
        return 2
    path = Path(args.file)
    text = path.read_text() if path.is_file() else None
    client = _client()
    doc = _read(client, s.site_bucket)
    diffs = differences(s, text, doc)
    if not diffs:
        print("the page has no changes that differ from the lane file")
        return 0
    print(f"page changes (revision {doc['revision']}) against {path}:")
    for lane, field, file_value, page_value in diffs:
        print(f"  {lane}.{field}: file {file_value}  page {page_value}")
    if args.command == "status":
        print("\nrun `fold` to keep the page's values in the file, or `drop` to discard them")
        return 1
    changes = {f"{lane}.{field}": {"from": fv, "to": pv} for lane, field, fv, pv in diffs}
    if args.command == "fold":
        new = text or ""
        for lane, field, _, page_value in diffs:
            new = set_in_toml(new, lane, field, page_value)
        lanes.lane_specs_from(s, new, None)  # the new file must still load
        shutil.copy(path, path.with_suffix(".toml.bak-sync"))
        path.write_text(new)
        rev = _clear(client, s.site_bucket, doc, "folded into strategies.toml", changes)
        print(f"\nwrote {len(diffs)} value(s) into {path} (backup {path.with_suffix('.toml.bak-sync')}); page changes cleared as revision {rev}. Now: scripts/cloud_push.sh")
    else:
        rev = _clear(client, s.site_bucket, doc, "dropped; the lane file stands", changes)
        print(f"\npage changes dropped as revision {rev}; the bot picks it up within a minute")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
