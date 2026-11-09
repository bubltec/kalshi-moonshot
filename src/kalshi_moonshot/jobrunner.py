"""The job container's entry point (infra/jobs_stack.py, scripts/cloud_job.sh): restore the newest nightly backups into the task's own Postgres, run one script, send the output back to the archive bucket.

Environment: JOB_ID, JOB_BUCKET, JOB_BUCKET_REGION (the archive bucket), JOB_ENV (space-separated NAME=value pairs the script should see), JOB_TIMEOUT (seconds, default 10800). DATABASE_URL, DB_SCHEMA_MAIN, DB_SCHEMA_TAPE and DATA_DIR come from the task definition.
Reads s3://BUCKET/backups/moonshot-*.dump and tape-*.dump (the newest of each) and jobs/<id>/script.py and jobs/<id>/strategies.toml (put there by the launcher). Writes jobs/<id>/out.txt, jobs/<id>/done.json and jobs/<id>/files/<name> for each file the script left in DATA_DIR.
Nothing here knows a lane, a market or a model: it is a runner."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("jobrunner")

COLLECT_MAX_BYTES = 20_000_000  # a file bigger than this stays in the task
KEEP_OUT = {"strategies.toml"}  # inputs, not results


def newest(keys: list[str], prefix: str) -> str | None:
    """The key under `prefix` with the latest date in its name (the backups are named NAME-YYYYMMDD.dump, so the sorted last is the newest). None when there is none."""
    found = sorted(k for k in keys if k.rsplit("/", 1)[-1].startswith(prefix) and k.endswith(".dump"))
    return found[-1] if found else None


def parse_job_env(text: str) -> dict[str, str]:
    """NAME=value pairs separated by spaces (quotes allowed) into a dict. A word without '=' is ignored."""
    out: dict[str, str] = {}
    for word in shlex.split(text or ""):
        if "=" in word:
            name, _, value = word.partition("=")
            if name.isidentifier():
                out[name] = value
    return out


def new_files(folder: Path, since: float) -> list[Path]:
    """The result files a script left in `folder`: written after `since`, small enough to send, and not an input."""
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.name not in KEEP_OUT and p.stat().st_mtime >= since and p.stat().st_size <= COLLECT_MAX_BYTES
    )


def _list_keys(s3: Any, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys += [o["Key"] for o in page.get("Contents", [])]
    return keys


def _wait_for_database(url: str, seconds: float = 180.0) -> None:
    import psycopg

    end = time.time() + seconds
    while True:
        try:
            psycopg.connect(url, connect_timeout=3).close()
            return
        except psycopg.OperationalError:
            if time.time() > end:
                raise
            time.sleep(2)


def main() -> int:
    import boto3

    job = os.environ["JOB_ID"]
    bucket = os.environ["JOB_BUCKET"]
    s3 = boto3.client("s3", region_name=os.environ.get("JOB_BUCKET_REGION", "us-east-1"))
    data = Path(os.environ.get("DATA_DIR", "/data"))
    data.mkdir(parents=True, exist_ok=True)
    work = Path("/tmp/job")
    work.mkdir(parents=True, exist_ok=True)
    started = time.time()
    info: dict[str, Any] = {"job": job, "started": started}

    def finish(code: int, note: str = "") -> int:
        info.update(exit=code, note=note, seconds=round(time.time() - started))
        s3.put_object(Bucket=bucket, Key=f"jobs/{job}/done.json", Body=json.dumps(info).encode())
        log.info("job %s finished with %d %s", job, code, note)
        return code

    try:
        url = os.environ["DATABASE_URL"]
        _wait_for_database(url)
        keys = _list_keys(s3, bucket, "backups/")
        restored = []
        for prefix in ("moonshot-", "tape-"):
            key = newest(keys, prefix)
            if key is None:
                return finish(2, f"no {prefix}*.dump in backups/")
            path = work / key.rsplit("/", 1)[-1]
            t = time.time()
            s3.download_file(bucket, key, str(path))
            subprocess.run(["pg_restore", "--no-owner", "--no-privileges", "-j", "4", "-d", url, str(path)], check=False)  # notices about extensions are not failures; a missing table shows when the script runs
            path.unlink()
            restored.append(key)
            log.info("restored %s in %.0fs", key, time.time() - t)
        info["restored"] = restored
        s3.download_file(bucket, f"jobs/{job}/script.py", str(work / "script.py"))
        s3.download_file(bucket, f"jobs/{job}/strategies.toml", str(data / "strategies.toml"))
    except Exception as exc:
        log.exception("setup failed")
        return finish(3, f"setup failed: {exc}")

    env = {**os.environ, **parse_job_env(os.environ.get("JOB_ENV", ""))}
    out = work / "out.txt"
    code = 0
    note = ""
    try:
        with out.open("w") as fh:
            proc = subprocess.Popen([sys.executable, "-u", str(work / "script.py")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, cwd=str(work))
            deadline = started + float(os.environ.get("JOB_TIMEOUT", "10800"))
            assert proc.stdout is not None
            for line in proc.stdout:
                fh.write(line)
                fh.flush()
                print(line, end="", flush=True)
                if time.time() > deadline:
                    proc.kill()
                    note = "timed out"
                    break
            code = proc.wait() if not note else 124
    except Exception as exc:
        code, note = 4, f"the script could not run: {exc}"
    s3.upload_file(str(out), bucket, f"jobs/{job}/out.txt")
    for path in new_files(data, started):
        s3.upload_file(str(path), bucket, f"jobs/{job}/files/{path.name}")
    return finish(code, note)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
