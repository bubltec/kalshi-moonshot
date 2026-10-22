"""Add `stop_loss = false` to the $1 ticket lanes in data/strategies.toml (idempotent). Run it after rebuilding.

    python3 scripts/no_stop_for_tickets.py [path/to/strategies.toml]
"""
import re
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/strategies.toml")
text = path.read_text()
for lane in ("late_ticket", "late_ticket_trail", "fx_ticket"):
    block = re.search(rf"^\[lane\.{lane}\]\n(?:(?!\[lane\.).*\n?)*", text, re.MULTILINE)
    if block and not re.search(r"^stop_loss\s*=", block.group(0), re.MULTILINE):
        text = text.replace(block.group(0), block.group(0).rstrip("\n") + "\nstop_loss = false\n\n", 1)
        print("added to", lane)
path.write_text(text)
