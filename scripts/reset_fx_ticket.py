"""Reset the live fx_ticket lane to your pattern and a fresh gate (idempotent). Run after rebuilding.

fx_ticket then buys the same thing fx_trail30 (paper) records: a currency side priced 25-35 cents, minutes 4-12, flat $1, peak-crash exit.
Its learned gate looks only at fx_trail30's bets from today on, so it stays closed (no buys) until 30 of them show a positive result.

    python3 scripts/reset_fx_ticket.py [path/to/strategies.toml] [YYYY-MM-DD]
"""
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/strategies.toml")
since = sys.argv[2] if len(sys.argv) > 2 else datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
text = path.read_text()
m = re.search(r"^\[lane\.fx_ticket\]\n(?:(?!\[lane\.).*\n?)*", text, re.MULTILINE)
if not m:
    raise SystemExit(f"no [lane.fx_ticket] in {path}")
block = m.group(0)
new = block
for key, value in (("price", "[0.25, 0.35]"), ("entry_elapsed", "[4, 12]")):
    new = re.sub(rf"^{key} = .*$", f"{key} = {value}", new, count=1, flags=re.MULTILINE)
new = re.sub(r"^(min_chance|max_chance|gate_from|gate_since)\s*=.*\n", "", new, flags=re.MULTILINE)
new = new.rstrip("\n") + f'\ngate_from = ["fx_trail30"]\ngate_since = "{since}"\n\n'
path.write_text(text.replace(block, new, 1))
print(f"fx_ticket reset: 25-35c entries, minutes 4-12, gate from fx_trail30 since {since}")
