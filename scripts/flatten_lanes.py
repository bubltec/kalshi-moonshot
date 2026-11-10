"""Flatten data/strategies.toml (2026-11-10, user: "we need to clean these lanes up. i can't even understand what a lane does because there's so much stuff turned on and off").
Every live lane becomes one self-contained section with no `inherit`, listing only the settings that differ from the LaneSpec defaults, grouped (what it trades / when and at what price it buys / size / how it sells / other gates). The rest of the lanes (switched-off and paper) are copied verbatim into a second file that nothing loads. The check that makes this safe: every live lane resolves to exactly the same settings before and after; any difference stops the write.

    python scripts/flatten_lanes.py                 # writes data/strategies.new.toml and data/strategies_retired.toml, prints the check
    python scripts/flatten_lanes.py --apply         # the check passed: replaces data/strategies.toml (the old file is kept as data/strategies.pre-cleanup.toml)
"""

import dataclasses
import json
import re
import shutil
import sys
import tomllib
from pathlib import Path

from kalshi_moonshot import config, lanes

root = Path(__file__).resolve().parent.parent
old_path = root / "data" / "strategies.toml"
new_path = root / "data" / "strategies.new.toml"
retired_path = root / "data" / "strategies_retired.toml"
s = config.reload()
old_text = old_path.read_text()
old = lanes.lane_specs_from(s, old_text, None)
raw = tomllib.loads(old_text)["lane"]
live = [n for n, l in old.items() if l.enabled and n in raw]
builtin = set(lanes.builtin_lanes(s))
keep_verbatim = [n for n in raw if n in builtin]  # tiny sections that switch a built-in lane off
retired = [n for n in raw if n not in live and n not in keep_verbatim]

fields = {f.name: f for f in dataclasses.fields(lanes.LaneSpec)}
REQUIRED = ("estimator", "label", "price", "hours", "max_spread", "min_volume")
WHAT = ("label", "series", "sides", "estimator", "needs_instrument", "numeric", "timed_suffix", "shadow_suffix", "one_per")
SIZE = re.compile(r"stake|bet_pct|exposure|max_open|kelly|disagree|bankroll|max_usd|contracts|scale_")
SELL = re.compile(r"^(shock|curve_exit|late_drop|value_exit|cross_|wrong_side|strike_through|stop_|burst|take_|exit_|rebuy|sell_|resting|manual|flip|hold_|giveback|trailing|settlement)")
BUY = re.compile(r"^(price|hours|entry_|min_|max_spread|always|priority|cooldown|review|scope|watch|shortlist|buy_|model_|record_|hot_|gate_|hour_drift|perp_lead|require_)")


def group(name):
    if name in WHAT:
        return "what it trades"
    if SELL.match(name):
        return "how it sells"
    if SIZE.search(name):
        return "size"
    if BUY.match(name) or name in REQUIRED:
        return "when and at what price it buys"
    return "other settings"


ORDER = ["what it trades", "when and at what price it buys", "size", "how it sells", "other settings"]


def toml_val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, (tuple, list)):
        return "[" + ", ".join(toml_val(x) for x in v) + "]"
    raise TypeError(f"cannot write {v!r}")


def section(name):
    spec = old[name]
    rows = {}
    for f in dataclasses.fields(lanes.LaneSpec):
        if f.name == "name":
            continue
        v = getattr(spec, f.name)
        default = f.default if f.default is not dataclasses.MISSING else None
        if f.name in REQUIRED or v != default:
            rows[f.name] = v
    groups = {g: [] for g in ORDER}
    for k, v in rows.items():
        groups[group(k)].append(f"{k} = {toml_val(v)}")
    out = [f"[lane.{name}]"]
    for g in ORDER:
        if groups[g]:
            out.append(f"# {g}")
            out += groups[g]
    return "\n".join(out)


head = """# Lanes: one self-contained section per live lane, [lane.NAME]. Nothing inherits from another lane. Each lists only what differs from the defaults in lanes.py, grouped:
# what it trades / when and at what price it buys / size / how it sells / other settings.
# Retired and paper lanes are in strategies_retired.toml (not loaded). The long history of every value is in strategies.pre-cleanup.toml and the commit messages.
# Check any change with  python -m kalshi_moonshot versions : a lane whose settings did not change keeps its version.
"""
blocks = [head]
for n in live:
    blocks.append(section(n))
for n in keep_verbatim:
    blocks.append(f"[lane.{n}]\n" + "\n".join(f"{k} = {toml_val(v)}" for k, v in raw[n].items()))
new_text = "\n\n".join(blocks) + "\n"
new_path.write_text(new_text)

# the retired file: each retired lane's section copied from the old text
parts = re.split(r"(?m)^(?=\[lane\.)", old_text)
keep = {n: p for p in parts if (m := re.match(r"\[lane\.([^\]]+)\]", p)) for n in [m.group(1)]}
retired_path.write_text("# Retired and paper lanes, copied unchanged from strategies.toml on 2026-11-10. NOT loaded by the bot. To bring one back, copy its section into strategies.toml.\n# Sections that say inherit = \"x\" need lane x's section copied with them.\n\n" + "".join(keep[n] for n in retired if n in keep))

new = lanes.lane_specs_from(s, new_text, None)
bad = []
for n in live + keep_verbatim:
    a, b = dataclasses.asdict(old[n]), dataclasses.asdict(new[n])
    diff = {k: (a[k], b[k]) for k in a if a[k] != b[k]}
    if diff:
        bad.append((n, diff))
enabled_old = sorted(n for n, l in old.items() if l.enabled)
enabled_new = sorted(n for n, l in new.items() if l.enabled)
print(f"old file {len(old_text.splitlines())} lines, new file {len(new_text.splitlines())} lines; live lanes {len(live)}; verbatim built-ins {keep_verbatim}; retired {len(retired)}")
print("enabled lanes identical:", enabled_old == enabled_new, enabled_new)
for n in live:
    print(f"  {n}: {sum(1 for ln in section(n).splitlines() if ' = ' in ln)} settings (was {len(raw[n])} of its own plus inherited)")
if bad:
    for n, d in bad:
        print("DIFFERENT", n, d)
    sys.exit(1)
print("every live lane resolves to identical settings: OK")
if "--apply" in sys.argv:
    shutil.copy(old_path, root / "data" / "strategies.pre-cleanup.toml")
    shutil.copy(new_path, old_path)
    print("applied; old file kept as data/strategies.pre-cleanup.toml")
