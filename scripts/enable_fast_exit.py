"""Add `trail_fast_drop = 0.12` (sell at once on a gap down of 12 cents from the held peak) to the peak-crash lanes in
data/strategies.toml (idempotent). Run it after rebuilding.

    python3 scripts/enable_fast_exit.py [path/to/strategies.toml] [dollars]
"""
import re
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/strategies.toml")
fast = sys.argv[2] if len(sys.argv) > 2 else "0.12"
text = path.read_text()
for lane in ("commodity", "fx_ticket", "late_ticket_trail"):
    block = re.search(rf"^\[lane\.{lane}\]\n(?:(?!\[lane\.).*\n?)*", text, re.MULTILINE)
    if block and re.search(r"^trail_drop\s*=", block.group(0), re.MULTILINE) and not re.search(r"^trail_fast_drop\s*=", block.group(0), re.MULTILINE):
        text = text.replace(block.group(0), block.group(0).rstrip("\n") + f"\ntrail_fast_drop = {fast}\n\n", 1)
        print("added to", lane)
path.write_text(text)
