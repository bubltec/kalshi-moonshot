import dataclasses, tomllib, re, os, collections
from kalshi_moonshot import config, lanes
s = config.load(); specs = lanes.lane_specs(s)
raw = tomllib.load(open("data/strategies.toml","rb"))["lane"]
fs = {f.name: (f.default if f.default is not dataclasses.MISSING else None) for f in dataclasses.fields(lanes.LaneSpec)}
L = raw["crypto_multiscale"]; sp = specs["crypto_multiscale"]
same = [k for k, v in L.items() if k in fs and (fs[k] == v or (isinstance(fs[k], tuple) and list(fs[k]) == v))]
print("crypto_multiscale: settings in file", len(L), "| equal to the code default:", len(same)); print("  ", ", ".join(same))
masters = {
 "curve exit (curve_exit = -1: off)": (sp.curve_exit < 0 and sp.curve_rest <= 0, ["curve_exit_persist","curve_exit_last","curve_exit_near","curve_exit_scales","curve_behind_last","curve_behind_chance","curve_gap","curve_gap_persist","curve_drop_units","curve_drop_seconds","curve_drop_margin_units","curve_drop_persist","curve_drop_comeback","curve_wait_severe"]),
 "shock exit (shock_bp/shock_units 0: off)": (sp.shock_bp <= 0 and sp.shock_units <= 0, ["shock_last","shock_persist","shock_memory","shock_seconds","shock_margin_bp","shock_bid_max"]),
 "burst exit (burst_exit false: off)": (not sp.burst_exit, ["burst_ratio","burst_dollars","burst_favorite","burst_share","burst_min_dollars","burst_window","burst_min_left"]),
 "timed drop exit (drop_cents 0: off)": (sp.drop_cents <= 0, ["drop_seconds","drop_confirm","drop_stay","drop_window","drop_was"]),
 "curve model buy (model_buy false: off)": (not sp.model_buy, ["model_top","model_features","model_category"]),
 "scope/review timers (entry_socket: event-driven)": (bool(sp.entry_socket), ["scope_minutes","review_minutes"]),
 "stop loss (stop_loss false: off)": (not sp.stop_loss, ["watch_loss_seconds","watch_loss_lift","stop_drop"]),
 "record adjust (record_adjust false)": (not sp.record_adjust, ["trust"]),
}
tot = 0
for label, (off, deps) in masters.items():
    got = [d for d in deps if d in L]
    if off and got:
        tot += len(got); print(f"INERT under {label}: {', '.join(f'{d}={L[d]!r}' for d in got)}")
print("inert settings on the live lane:", tot, "of", len(L))
# same across all lanes
inert_all = collections.Counter()
for name, cfg in raw.items():
    spx = specs.get(name)
    if spx is None: continue
    for label, deps in (("burst", masters["burst exit (burst_exit false: off)"][1]),):
        if not spx.burst_exit: inert_all["burst"] += sum(1 for d in deps if d in cfg)
print("burst settings across all lanes with burst_exit off:", inert_all["burst"])
print("total settings lines in the lane file:", sum(len(c) for c in raw.values()), "| comment lines:", sum(1 for l in open('data/strategies.toml') if l.strip().startswith('#')), "| bytes:", os.path.getsize('data/strategies.toml'))
# env flags
env = [l.split("=",1)[0] for l in open(".env").read().splitlines() if "=" in l and not l.strip().startswith("#")]
print(".env settings:", len(env), "| config.py Settings fields:", len(dataclasses.fields(config.Settings)))
# threads and recurring jobs
import subprocess
out = subprocess.run("grep -rn 'threading.Thread(' src/kalshi_moonshot/*.py | wc -l; grep -rhn 'name=\"[a-z-]*\"' src/kalshi_moonshot/*.py | grep -o 'name=\"[a-z-]*\"' | sort | uniq -c | sort -rn | head -60", shell=True, capture_output=True, text=True).stdout
print("threads started (call sites) and their names:\n" + out)
