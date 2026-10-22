"""Give a lane a per-market learned gate (idempotent). Run it after rebuilding.

Each market (series) then keeps its own record per minute band, blended toward the lane's pooled result: a market with few bets
borrows the pool's verdict, and a market with its own bad record is closed even when the pool is open.

    python3 scripts/enable_series_gate.py [lane] [path/to/strategies.toml]
"""
import re
import sys
from pathlib import Path

lane = sys.argv[1] if len(sys.argv) > 1 else "fx_ticket"
path = Path(sys.argv[2] if len(sys.argv) > 2 else "data/strategies.toml")
text = path.read_text()
m = re.search(rf"^\[lane\.{re.escape(lane)}\]\n(?:(?!\[lane\.).*\n?)*", text, re.MULTILINE)
if not m:
    raise SystemExit(f"no [lane.{lane}] in {path}")
block = m.group(0)
if not re.search(r"^gate_from\s*=", block, re.MULTILINE):
    raise SystemExit(f"{lane} has no gate_from: give it a learned gate first")
new = re.sub(r"^gate_by\s*=.*\n", "", block, flags=re.MULTILINE).rstrip("\n") + '\ngate_by = "series"\ngate_pool = 30\n\n'
path.write_text(text.replace(block, new, 1))
print(f"{lane}: per-market gate on (each market blended toward the pool, 30 bets of prior)")
