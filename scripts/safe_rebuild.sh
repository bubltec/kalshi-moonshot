#!/usr/bin/env bash
# Rebuild and restart the bot only when nothing real is open, so a restart never leaves a position unwatched.
# Waits until `python -m kalshi_moonshot flat` reports no open real position (the bot's own live bets or hand-placed ones),
# then runs `docker compose up -d --build`.
#   scripts/safe_rebuild.sh            wait as long as it takes
#   scripts/safe_rebuild.sh --force    rebuild now, whatever is open
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${1:-}" != "--force" ] && docker compose ps --status running --quiet | grep -q .; then
  until out=$(docker compose exec -T bot python -m kalshi_moonshot flat 2>&1); do
    echo "$(date +%H:%M:%S) $(echo "$out" | tail -1); waiting to rebuild..."
    sleep 20
  done
fi
docker compose up -d --build
