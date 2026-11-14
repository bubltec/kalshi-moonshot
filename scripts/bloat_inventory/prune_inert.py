"""Remove settings from data/strategies.toml that cannot change what a lane does (2026-11-14 cleanup). One kind only: a knob of an exit whose master switch is off on that lane, where every read of the knob sits behind that switch (checked by hand, watcher.py and variants.py).
A line that only repeats the code's default is kept: several of those record a decision (model_buy = false, always = false), and a later change of the default must not flip a lane.
      burst_* with burst_exit false; the timed drop's drop_seconds/confirm/stay/window with drop_cents 0; shock_last/persist/memory with no shock exit (shock_bp 0, shock_units 0, cross_units under 0);
      the curve exit's persist/near/last/behind/drop/gap knobs with curve_exit under 0 and curve_rest 0; watch_loss_seconds/lift with watch_loss 0.
It then reloads the file and checks that no lane changed in any field outside (b). DRY=1 prints what it would remove. Run from the repo root on the Mac."""
import dataclasses
import os
import re
import tomllib

from kalshi_moonshot import config, lanes

PATH = "data/strategies.toml"
FAMILIES = {
    "burst": (lambda sp: not sp.burst_exit, ["burst_ratio", "burst_dollars", "burst_favorite", "burst_share", "burst_min_dollars", "burst_window", "burst_min_left", "burst_dollars_btc", "burst_spot_edge"]),
    "drop": (lambda sp: sp.drop_cents <= 0, ["drop_seconds", "drop_confirm", "drop_stay", "drop_window"]),
    "shock": (lambda sp: sp.shock_bp <= 0 and sp.shock_units <= 0 and sp.cross_units < 0, ["shock_last", "shock_persist", "shock_memory"]),
    "curve": (lambda sp: sp.curve_exit < 0 and sp.curve_rest <= 0, ["curve_exit_persist", "curve_exit_near", "curve_exit_last", "curve_behind_last", "curve_behind_chance", "curve_drop_units", "curve_drop_seconds", "curve_drop_margin_units", "curve_drop_persist", "curve_gap", "curve_gap_persist"]),
    "watch_loss": (lambda sp: sp.watch_loss <= 0, ["watch_loss_seconds", "watch_loss_lift"]),
}
s = config.load()
before = lanes.lane_specs(s)
raw = tomllib.load(open(PATH, "rb"))["lane"]
drop: dict[str, dict[str, str]] = {}
for name, cfg in raw.items():
    sp = before[name]
    for key, val in cfg.items():
        if key in ("enabled", "label", "series"):
            continue
        for fam, (off, keys) in FAMILIES.items():
            if key in keys and off(sp):
                drop.setdefault(name, {})[key] = fam
gated = {k for _, keys in FAMILIES.values() for k in keys}
text, out, lane, removed = open(PATH).read().splitlines(keepends=True), [], None, 0
for line in text:
    m = re.match(r"^\[lane\.([A-Za-z0-9_]+)\]", line)
    if m:
        lane = m.group(1)
    k = re.match(r"^([a-z_0-9]+)\s*=", line)
    if lane and k and k.group(1) in drop.get(lane, {}):
        removed += 1
        continue
    out.append(line)
for name in drop:
    print(f"{name}: {len(drop[name])} -> " + ", ".join(f"{k} ({why})" for k, why in sorted(drop[name].items())))
print(f"lines removed: {removed} of {len(text)}")
if os.environ.get("DRY"):
    raise SystemExit(0)
open(PATH, "w").write("".join(out))
after = lanes.lane_specs(config.load())
assert set(after) == set(before), "a lane appeared or vanished"
for name in before:
    for f in dataclasses.fields(lanes.LaneSpec):
        a, b = getattr(before[name], f.name), getattr(after[name], f.name)
        if a != b:
            assert f.name in gated and f.name in drop.get(name, {}), f"{name}.{f.name} changed from {a!r} to {b!r} and is not a gated knob"
print("checked: every lane is the same in every field except the removed gated knobs")
