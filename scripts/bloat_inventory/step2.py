import ast, os, re, subprocess
root = "src/kalshi_moonshot"
mods = {p[:-3] for p in os.listdir(root) if p.endswith(".py")}
lines = {m: sum(1 for _ in open(f"{root}/{m}.py")) for m in mods}
def imports(path, pkg=True):
    out = set()
    try: tree = ast.parse(open(path).read())
    except SyntaxError: return out
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            if n.level >= 1 and pkg:
                if n.module: out.add(n.module.split(".")[0])
                else: out |= {a.name for a in n.names}
            elif n.module and n.module.startswith("kalshi_moonshot"):
                parts = n.module.split(".")
                if len(parts) > 1: out.add(parts[1])
                else: out |= {a.name for a in n.names}
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.startswith("kalshi_moonshot."): out.add(a.name.split(".")[1])
    return out & mods
graph = {m: imports(f"{root}/{m}.py") for m in mods}
def reach(starts):
    seen, todo = set(), list(starts)
    while todo:
        m = todo.pop()
        if m in seen or m not in mods: continue
        seen.add(m); todo += list(graph[m])
    return seen
# entry: what does the container run?
print("compose command / entry:")
os.system("grep -n 'command:\\|entrypoint\\|CMD\\|ENTRYPOINT' docker-compose.yml Dockerfile 2>/dev/null | head -8")
bot = reach({"__main__"})
script_imp, test_imp = set(), set()
for d, acc in (("scripts", script_imp), ("tests", test_imp)):
    for p in os.listdir(d):
        if p.endswith(".py"): acc |= imports(f"{d}/{p}", pkg=False)
only_scripts = (reach(script_imp) - bot)
only_tests = (reach(test_imp) - bot - only_scripts)
nothing = mods - bot - reach(script_imp) - reach(test_imp)
print(f"\nmodules: {len(mods)}, lines: {sum(lines.values())}")
print(f"reachable from __main__: {len(bot)} modules, {sum(lines[m] for m in bot)} lines")
for label, group in (("only from scripts", only_scripts), ("only from tests", only_tests), ("from nothing", nothing)):
    print(f"\n{label}: {len(group)} modules, {sum(lines[m] for m in group)} lines")
    for m in sorted(group, key=lambda m: -lines[m]): print(f"  {m}.py {lines[m]}")
print("\nlargest reachable modules:")
for m in sorted(bot, key=lambda m: -lines[m])[:25]: print(f"  {m}.py {lines[m]}  imported by {sum(1 for k in mods if m in graph[k])}")
print("\nscripts:", len([p for p in os.listdir('scripts')]), "| results:", len(os.listdir('studies/results')), "| tests:", len([p for p in os.listdir('tests') if p.startswith('test_')]))
run = reach({"worker", "loop"} & mods or {"worker"})
print(f"\nreachable from the worker (the running bot): {len(run)} modules, {sum(lines[m] for m in run)} lines")
cli = bot - run
print(f"reachable only through __main__ commands (not the running bot): {len(cli)} modules, {sum(lines[m] for m in cli)} lines")
for m in sorted(cli, key=lambda m: -lines[m]): print(f"  {m}.py {lines[m]}")
