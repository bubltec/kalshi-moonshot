"""Switch the live fx_ticket lane from a fixed entry window to the learned gate (idempotent). Run after rebuilding.

    python3 scripts/enable_learned_gate.py [path/to/strategies.toml]
Sets entry_elapsed to the whole window and adds gate_from, so `gate --lane fx_ticket` decides which minutes may trade.
"""
import re
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/strategies.toml")
text = path.read_text()
m = re.search(r"^\[lane\.fx_ticket\]\n(?:(?!\[lane\.).*\n?)*", text, re.MULTILINE)
if not m:
    raise SystemExit(f"no [lane.fx_ticket] in {path}")
block = m.group(0)
new = re.sub(r"^entry_elapsed = .*$", "entry_elapsed = [1, 14]          # the whole window: the learned gate decides which minutes may trade",
             block, count=1, flags=re.MULTILINE)
if not re.search(r"^gate_from\s*=", new, re.MULTILINE):
    new = new.rstrip("\n") + '\ngate_from = ["late_ticket", "late_ticket_trail", "fx_ticket"]\ngate_min_n = 30\ngate_z = 1.0\n\n'
path.write_text(text.replace(block, new, 1))
print("fx_ticket now uses the learned gate")
