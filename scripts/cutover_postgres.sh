#!/usr/bin/env bash
# Move the live bot from SQLite to Postgres, once, when nothing is open.
#
#   1. waits until no position is open (a restart blinds the watcher for about 30 seconds)
#   2. stops the bot
#   3. takes a final consistent SQLite copy of both databases (data/backups/*-final.db), the way the bot stored them
#   4. starts Postgres and waits for it to be healthy
#   5. copies every table across (scripts/migrate_to_postgres.py, which checks row counts and sums), replacing anything already there
#   6. starts the bot on the new build, which now uses DATABASE_URL
#
# The SQLite files and the tape volume are left exactly as they were: they are your rollback (remove DATABASE_URL from docker-compose.yml
# and `docker compose up -d`). Run it with --force to skip waiting for a flat account.
set -euo pipefail
cd "$(dirname "$0")/.."

flat() {
  docker compose exec -T bot python -m kalshi_moonshot manual 2>/dev/null | grep -q "no hand-placed open positions" || return 1
  [ "$(sqlite3 -readonly data/moonshot.db "select count(*) from decisions where action='bet' and mode='trade' and outcome is null and exit_ts is null and close_time > strftime('%Y-%m-%dT%H:%M:%SZ','now')")" = "0" ]
}

echo "building the new image (the bot keeps running meanwhile)"
docker compose build bot

if [ "${1:-}" != "--force" ]; then
  until flat; do echo "$(date +%H:%M:%S) positions open; waiting..."; sleep 15; done
fi
echo "flat: starting the move"
docker compose stop bot

echo "final SQLite copy"
docker compose run --rm -T --no-deps -e DATABASE_URL= bot python - <<'PY'
from pathlib import Path
from kalshi_moonshot import backup, config
s = config.load()
for name, src in (("moonshot", s.db_path), ("tape", s.tape_path)):
    dest = s.data_dir / "backups" / f"{name}-final.db"
    print(name, "ok" if backup.copy_db(src, dest) else "FAILED")
PY
test -s data/backups/moonshot-final.db && test -s data/backups/tape-final.db

echo "starting Postgres"
docker compose up -d db
until [ "$(docker compose ps db --format '{{.Health}}')" = "healthy" ]; do sleep 2; done

echo "copying"
docker compose run --rm -T --no-deps -v "$PWD/scripts:/scripts:ro" bot python /scripts/migrate_to_postgres.py \
  /data/backups/moonshot-final.db /data/backups/tape-final.db --wipe

echo "starting the bot on Postgres"
docker compose up -d
echo "moved to Postgres"
