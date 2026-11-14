#!/usr/bin/env bash
# The bloat inventory of 2026-11-14: what the bot uses against what it carries. Read-only. Run from the repo root on the Mac:
#   scripts/bloat_inventory/run.sh > studies/results/<date>-bloat-inventory-raw.txt
# step1 lanes and settings, step2 modules, step3 dead settings per module, step4 exit checks, step5 the live lane's inert settings and the threads.
set -euo pipefail
cd "$(dirname "$0")/../.."
export SP="${TMPDIR:-/tmp}"; export OUT="$SP/inv1.json"
for n in 1 2 3 4 5; do echo "===== step $n ====="; .venv/bin/python "scripts/bloat_inventory/step$n.py"; done
