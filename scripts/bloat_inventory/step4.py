import dataclasses, json, os, re
from kalshi_moonshot import config, lanes
s = config.load(); specs = lanes.lane_specs(s)
inv = json.load(open(os.environ["SP"] + "/inv1.json")); running = inv["running"]; live = [n for n, st in inv["status"].items() if st == "LIVE"]
OFFV = (0, 0.0, False, None, "", (), -1, -1.0, [], {})
fs = {f.name: (f.default if f.default is not dataclasses.MISSING else None) for f in dataclasses.fields(lanes.LaneSpec)}
never_off = [n for n in inv["never"] if fs[n] in OFFV or fs[n] == () ]
never_on = [n for n in inv["never"] if n not in never_off]
print("never set, default is OFF (a feature no lane uses):", len(never_off))
print("never set, default is a working value (a constant nobody tunes):", len(never_on)); print("  ", ", ".join(f"{n}={fs[n]!r}" for n in never_on))
print("\nset only by lanes that are off:", ", ".join(inv["off_only"]))
w = open("src/kalshi_moonshot/watcher.py").read()
print("\n== exit checks: first guard, and the running lanes that arm it ==")
def lanes_with(pred): return [n for n in running if pred(specs[n])]
for m in re.finditer(r"    def (_[a-z_]+_hit)\(self[^\n]*\n((?:.*\n){1,6})", w):
    body = m.group(2)
    guard = next((l.strip() for l in body.splitlines() if l.strip().startswith("if ")), "")
    start = w.count("\n", 0, m.start()) + 1
    end_m = re.search(r"\n    def ", w[m.end():]); n_lines = w[m.end(): m.end() + (end_m.start() if end_m else 0)].count("\n") + 1
    print(f"{m.group(1)} (watcher.py:{start}, {n_lines} lines): {guard[:150]}")
checks = {
 "_stop_hit / stop_loss": lambda sp: sp.stop_loss, "_drop_hit / drop_cents": lambda sp: sp.drop_cents > 0, "_bounce_hit / bounce_fall": lambda sp: sp.bounce_fall > 0,
 "_unstable_hit / unstable_gain": lambda sp: sp.unstable_gain > 0, "_hover_hit / hover_dip": lambda sp: sp.hover_dip > 0, "_risk (curve_risk_sell)": lambda sp: sp.curve_risk_sell > 0,
 "_risk LIVE (curve_risk_live)": lambda sp: sp.curve_risk_sell > 0 and sp.curve_risk_live, "_strike_through_hit": lambda sp: sp.strike_through_sale > 0, "_cross_take_hit": lambda sp: sp.cross_take > 0,
 "_shock_hit (shock_bp/units)": lambda sp: sp.shock_bp > 0 or sp.shock_units > 0, "_wrong_side_hit": lambda sp: sp.wrong_side_bp > 0, "_late_drop_hit": lambda sp: sp.late_drop_units > 0,
 "_settlement_hit (settlement_guard)": lambda sp: sp.settlement_guard > 0, "_value_hit": lambda sp: sp.value_exit > 0 or sp.value_exit_gain_x > 0 or sp.value_exit_high > 0,
 "_curve_hit (curve_exit>=0)": lambda sp: sp.curve_exit >= 0, "_curve_rest_hit (curve_rest)": lambda sp: sp.curve_rest > 0, "take_profit_x/at": lambda sp: sp.take_profit_x > 0 or sp.take_profit_at > 0,
 "cash_out_at": lambda sp: sp.cash_out_at > 0, "scalp_at/up": lambda sp: sp.scalp_at > 0 or sp.scalp_up > 0, "trail_drop": lambda sp: sp.trail_drop > 0, "spike_cents": lambda sp: sp.spike_cents > 0,
 "cash_up/cash_drop/cash_left": lambda sp: sp.cash_up_minute > 0 or sp.cash_drop > 0 or sp.cash_left_seconds > 0, "burst (burst_ratio/dollars)": lambda sp: sp.burst_ratio > 0 or sp.burst_dollars > 0,
 "curve_take": lambda sp: sp.curve_take, "squeeze": lambda sp: sp.squeeze_seconds > 0, "curve_risk_swing": lambda sp: sp.curve_risk_swing > 0, "curve_risk_stop_hold": lambda sp: sp.curve_risk_stop_hold > 0,
}
print("\n== which running lanes arm each exit (LIVE lanes in caps) ==")
for k, pred in checks.items():
    try: got = lanes_with(pred)
    except AttributeError as e: print(f"  {k}: field missing ({e})"); continue
    print(f"  {k}: {len(got)} -> {', '.join(n.upper() if n in live else n for n in got) or 'NO RUNNING LANE'}")
