import dataclasses, os, re, subprocess, tomllib
from kalshi_moonshot import config, lanes
s = config.load()
specs = lanes.lane_specs(s)
env = dict(l.split("=",1) for l in open(".env").read().splitlines() if "=" in l and not l.strip().startswith("#"))
live = [x.strip() for x in env.get("LIVE_LANES","").split(",") if x.strip()]
paper = [x.strip() for x in env.get("PAPER_LANE_NAMES","").split(",") if x.strip()]
raw = tomllib.load(open("data/strategies.toml","rb"))["lane"]
print("LANES in file:", len(raw), "| specs loaded:", len(specs))
status = {}
for name, sp in specs.items():
    st = "LIVE" if name in live else "PAPER-RUNNING" if name in paper and sp.enabled else "OFF"
    if not sp.enabled: st += " (enabled=false)"
    status[name] = st
    print(f"  {st:28s} {name}  [{len(raw.get(name,{}))} settings in file]")
print("paper names with no lane in the file:", [p for p in paper if p not in specs])
print("live names with no lane in the file:", [p for p in live if p not in specs])
running = [n for n, st in status.items() if st.startswith(("LIVE","PAPER-RUNNING"))]
fields = [f for f in dataclasses.fields(lanes.LaneSpec)]
default = {}
for f in fields:
    default[f.name] = f.default if f.default is not dataclasses.MISSING else (f.default_factory() if f.default_factory is not dataclasses.MISSING else None)
never, off_only, used = [], [], []
src = {p: open(os.path.join("src/kalshi_moonshot", p)).read() for p in os.listdir("src/kalshi_moonshot") if p.endswith(".py")}
def readers(name):
    out = {}
    pat = re.compile(r"\.%s\b" % re.escape(name))
    for p, text in src.items():
        if p == "lanes.py": continue
        n = len(pat.findall(text))
        if n: out[p] = n
    return out
rows = []
for f in fields:
    setters = [n for n, sp in specs.items() if getattr(sp, f.name) != default[f.name]]
    run = [n for n in setters if n in running]
    rd = readers(f.name)
    rows.append((f.name, setters, run, rd))
never = [r for r in rows if not r[1]]
off_only = [r for r in rows if r[1] and not r[2]]
print("\nFIELDS:", len(fields), "| set by a running lane:", len(rows)-len(never)-len(off_only), "| set only by lanes that are off:", len(off_only), "| set by no lane:", len(never))
print("\n== set by NO lane (name: files that read it) ==")
for name, _, _, rd in never:
    print(f"  {name}: {sum(rd.values())} reads in {', '.join(sorted(rd)) or 'NOTHING'}")
print("\n== set ONLY by lanes that are off ==")
for name, setters, _, rd in off_only:
    print(f"  {name}: set by {', '.join(setters)}; {sum(rd.values())} reads in {', '.join(sorted(rd)) or 'NOTHING'}")
import json
json.dump({"status": status, "never": [r[0] for r in never], "off_only": [r[0] for r in off_only], "running": running}, open(os.environ.get("OUT","/tmp/inv1.json"),"w"))
