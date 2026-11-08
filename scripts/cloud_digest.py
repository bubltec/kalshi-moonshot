"""The daily digest, read from the live host (2026-11-08). `python -m kalshi_moonshot digest` reads the database it is pointed at; this Mac's Docker Postgres stopped at 2026-11-02, so the review reads the host's instead.

    scripts/cloud.sh run scripts/cloud_digest.py [ARGS="--days 7"]
Read-only. Prints what `python -m kalshi_moonshot digest [ARGS]` prints, inside the bot container on the host."""

import os
import sys

from kalshi_moonshot.__main__ import main

raise SystemExit(main(["kalshi_moonshot", "digest", *os.environ.get("ARGS", "").split()]))
