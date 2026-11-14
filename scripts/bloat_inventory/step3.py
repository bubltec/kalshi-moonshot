import dataclasses, json, os, re, collections
from kalshi_moonshot import config, lanes
s = config.load(); specs = lanes.lane_specs(s)
inv = json.load(open(os.environ["SP"] + "/inv1.json"))
running = inv["running"]
print("estimators of running lanes:", collections.Counter(specs[n].estimator for n in running))
print("estimators of every lane:", collections.Counter(sp.estimator for sp in specs.values()))
src = {p: open("src/kalshi_moonshot/" + p).read() for p in os.listdir("src/kalshi_moonshot") if p.endswith(".py")}
dead = set(inv["never"]) | set(inv["off_only"])
# per module: how many distinct dead fields it reads, and how many live ones
per = collections.defaultdict(lambda: [set(), set()])
allf = [f.name for f in dataclasses.fields(lanes.LaneSpec)]
for f in allf:
    pat = re.compile(r"(?:spec|sp|lane|self\.spec|t)\.%s\b" % re.escape(f))
    for p, text in src.items():
        if p == "lanes.py": continue
        if pat.search(text): per[p][0 if f in dead else 1].add(f)
print("\nmodule: dead fields read / live fields read")
for p, (d, l) in sorted(per.items(), key=lambda kv: -len(kv[1][0])):
    if d: print(f"  {p}: {len(d)} dead / {len(l)} live   e.g. {', '.join(sorted(d)[:8])}")
# watcher exit chain
w = src["watcher.py"]
hits = sorted(set(re.findall(r"def (_[a-z_]+_hit)\(", w)))
print("\nwatcher exit checks (_*_hit):", len(hits)); print(" ", ", ".join(hits))
# Target fields
tf = re.findall(r"^    ([a-z_0-9]+): [^=\n]+ = ", w[w.index("class Target"):w.index("class Target")+30000], re.M)
print("Target fields:", len(tf))
# dead field families
fam = collections.Counter(re.match(r"[a-z]+(?:_[a-z]+)?", f).group(0) for f in dead)
print("\ndead fields by prefix:", dict(fam.most_common(45)))
