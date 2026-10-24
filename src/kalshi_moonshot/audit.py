"""A read-only audit of the code and the configuration, for the scheduled engineering review.

`python -m kalshi_moonshot audit` looks for what makes a system like this drift: settings nothing reads or that are only in one of `.env`, `.env.example` and
the code, lane fields no lane uses, functions nothing calls, the same lines written in several places, modules and functions that have grown too big,
modules no test touches, and whether the RAG index and the model's context builder are healthy. It reports candidates, not verdicts: a person (or the
review) reads each against the code before changing anything. It changes nothing.

The code checks need the source tree (the repository root, found from this file), so they are skipped inside the bot's container, which has only the
installed package; the database checks run anywhere.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any

from . import lanes
from .config import Settings

DUP_LINES = 6  # consecutive meaningful lines that must repeat to count as duplicated code
SKIP_NAMES = {"main", "cli", "build_parser", "setup", "teardown"}
# Variables that are read by something other than this code (boto3, docker compose, the shell), so not finding them in the source is expected.
EXTERNAL_VARS = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION", "POSTGRES_PASSWORD", "POSTGRES_USER", "POSTGRES_DB"}


def repo_root() -> Path | None:
    root = Path(__file__).resolve().parents[2]
    return root if (root / "src" / "kalshi_moonshot").is_dir() else None


def _py(root: Path, *parts: str) -> list[Path]:
    return sorted((root.joinpath(*parts)).rglob("*.py")) if root.joinpath(*parts).is_dir() else []


def _text(paths: list[Path]) -> dict[Path, str]:
    return {p: p.read_text() for p in paths}


# ---- configuration ----
def env_names_in(source: str) -> set[str]:
    """Environment variable names a module reads: quoted ALL_CAPS names passed to the config helpers or to os.environ."""
    names = set(re.findall(r'(?:_[fbis]|_csv|_sides|environ\.get|getenv)\(\s*"([A-Z][A-Z0-9_]{2,})"', source))
    names |= set(re.findall(r'environ\[\s*"([A-Z][A-Z0-9_]{2,})"', source))
    return names


def env_file_keys(path: Path) -> set[str]:
    """KEY names in a .env-style file, including commented-out `# KEY=` examples. Values are never read into the report."""
    if not path.is_file():
        return set()
    return set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]{2,})=", path.read_text(), flags=re.MULTILINE))


def settings_audit(root: Path, s: Settings) -> list[str]:
    src = _text(_py(root, "src", "kalshi_moonshot"))
    allsrc = "\n".join(src.values())
    out: list[str] = []
    unused = [f.name for f in fields(Settings) if not re.search(rf"\.{f.name}\b", allsrc)]
    out.append("settings fields nothing reads (candidates to remove): " + (", ".join(unused) or "none"))
    read = set().union(*(env_names_in(t) for t in src.values()))
    documented = env_file_keys(root / ".env.example")
    live = env_file_keys(root / ".env")
    out.append("variables the code reads that .env.example does not document: " + (", ".join(sorted(read - documented)) or "none"))
    out.append("variables .env.example documents that the code does not read (stale docs): " + (", ".join(sorted(documented - read - EXTERNAL_VARS)) or "none"))
    out.append("keys in your .env that the code does not read (deprecated or mistyped; names only): " + (", ".join(sorted(live - read - EXTERNAL_VARS)) or "none"))
    unused_lane = [f.name for f in fields(lanes.LaneSpec) if f.name != "name" and not re.search(rf"\.{f.name}\b", allsrc.replace("lanes.py", ""))]
    out.append("lane fields nothing outside lanes.py reads: " + (", ".join(unused_lane) or "none"))
    # fields set in strategies.toml that equal the default (noise), and fields no lane sets away from the default (maybe removable)
    toml = s.data_dir / "strategies.toml"
    if toml.is_file():
        import tomllib

        rows = tomllib.loads(toml.read_text()).get("lane") or {}
        defaults = {f.name: f.default for f in fields(lanes.LaneSpec) if f.default is not MISSING}
        used: set[str] = set()
        noise: list[str] = []
        for name in rows:
            row = lanes._inherited(rows, name)
            for k, v in row.items():
                used.add(k)
                if k in defaults and isinstance(defaults[k], (int, float, str, bool)) and defaults[k] == v and k != "enabled":
                    noise.append(f"{name}.{k}")
        out.append("strategies.toml settings that equal the default (can be dropped): " + (", ".join(noise[:20]) + (" ..." if len(noise) > 20 else "") if noise else "none"))
        never = sorted(f.name for f in fields(lanes.LaneSpec) if f.name not in used and f.name != "name")
        out.append(f"lane fields no lane sets in strategies.toml ({len(never)}; built-in lanes may still use them): " + ", ".join(never[:30]) + (" ..." if len(never) > 30 else ""))
    return out


# ---- code ----
def dead_code(root: Path) -> list[str]:
    src, others = _text(_py(root, "src", "kalshi_moonshot")), _text(_py(root, "tests") + _py(root, "scripts"))
    all_src = "\n".join(src.values())
    all_other = "\n".join(others.values())
    unused: list[str] = []
    test_only: list[str] = []
    for path, text in src.items():
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
                if name.startswith(("__", "test_")) or name in SKIP_NAMES:
                    continue
                n_src = len(re.findall(rf"\b{re.escape(name)}\b", all_src))
                if n_src <= 1:
                    (test_only if re.search(rf"\b{re.escape(name)}\b", all_other) else unused).append(f"{path.name}:{node.lineno} {name}")
    return [
        f"defined and never referenced anywhere ({len(unused)}): " + ("; ".join(unused[:25]) + (" ..." if len(unused) > 25 else "") if unused else "none"),
        f"referenced only by tests or scripts, not by the bot ({len(test_only)}): " + ("; ".join(test_only[:25]) + (" ..." if len(test_only) > 25 else "") if test_only else "none"),
    ]


def _meaningful(line: str) -> str | None:
    t = line.strip()
    if len(t) < 14 or t.startswith(("#", "import ", "from ", '"""', "'''", "@")) or t in {"else:", "try:", "finally:", "return", "pass", "continue", "break"}:
        return None
    return re.sub(r"\s+", " ", t)


def duplication(root: Path, top: int = 8) -> list[str]:
    seen: dict[tuple[str, ...], list[tuple[str, int]]] = defaultdict(list)
    for path in _py(root, "src", "kalshi_moonshot"):
        sig = [(i + 1, m) for i, ln in enumerate(path.read_text().splitlines()) if (m := _meaningful(ln))]
        for i in range(len(sig) - DUP_LINES + 1):
            seen[tuple(m for _, m in sig[i : i + DUP_LINES])].append((path.name, sig[i][0]))
    groups: dict[tuple[str, ...], list[tuple[str, int]]] = {k: v for k, v in seen.items() if len(set(v)) > 1}
    # merge overlapping windows of one repeated block by its first occurrence
    blocks: dict[tuple[tuple[str, int], ...], int] = {}
    for locs in groups.values():
        key = tuple(sorted(set(locs)))
        blocks[key] = blocks.get(key, 0) + 1
    ranked = sorted(blocks.items(), key=lambda kv: -(kv[1] * len(kv[0])))
    covered: set[tuple[str, int]] = set()
    lines = []
    for where, windows in ranked:
        if any(abs(loc[1] - c[1]) < 12 and loc[0] == c[0] for loc in where for c in covered):
            continue
        covered.update(where)
        lines.append(f"~{windows + DUP_LINES - 1} similar lines repeated at: " + ", ".join(f"{f}:{n}" for f, n in where[:5]))
        if len(lines) >= top:
            break
    return lines or [f"no repeated blocks of {DUP_LINES} or more lines"]


def size_report(root: Path, top: int = 8) -> list[str]:
    mods = sorted(((len(p.read_text().splitlines()), p.name) for p in _py(root, "src", "kalshi_moonshot")), reverse=True)[:top]
    funcs: list[tuple[int, str]] = []
    for p in _py(root, "src", "kalshi_moonshot"):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno:
                funcs.append((node.end_lineno - node.lineno + 1, f"{p.name}:{node.lineno} {node.name}"))
    funcs.sort(reverse=True)
    return ["largest modules (lines): " + ", ".join(f"{n} {m}" for n, m in mods), "longest functions (lines): " + "; ".join(f"{n} {w}" for n, w in funcs[:top])]


def test_reach(root: Path) -> list[str]:
    tests = "\n".join(_text(_py(root, "tests")).values())
    names = {p.stem for p in _py(root, "tests")}
    untouched = [
        p.stem for p in _py(root, "src", "kalshi_moonshot")
        if p.stem not in ("__init__", "__main__") and not re.search(rf"\b{re.escape(p.stem)}\b", tests) and not any(n.startswith(f"test_{p.stem}") for n in names)
    ]
    n_tests = len(re.findall(r"^def test_", tests, flags=re.MULTILINE))
    return [f"{n_tests} test functions; source modules no test file mentions ({len(untouched)}): " + (", ".join(untouched) or "none")]


# ---- RAG and the model's context ----
def rag_health(store: Any, s: Settings) -> list[str]:
    from .report import first_per_market

    out = []
    cases = store.db.execute("SELECT COUNT(*) FROM rag_cases").fetchone()[0]
    settled = store.settled_decisions()
    global_first = [r for r in first_per_market(settled) if r["lane"] != "commodity"]
    per_lane: dict[str, list] = defaultdict(list)
    for r in settled:
        per_lane[r["lane"] or "moonshot"].append(r)
    lane_first = [r for rows in per_lane.values() for r in first_per_market(rows) if r["lane"] != "commodity"]
    out.append(f"RAG index: {cases:,} cases; {len(settled):,} settled decisions; eligible one-per-market-across-lanes {len(global_first):,}, one-per-market-per-lane {len(lane_first):,} (the index uses the first)")
    bySeries = store.db.execute("SELECT series, COUNT(*) AS n FROM rag_cases GROUP BY series ORDER BY n DESC LIMIT 8").fetchall()
    out.append("cases by series (top 8): " + ", ".join(f"{r['series'] or '?'} {r['n']}" for r in bySeries))
    llm_lanes = sorted(sp.name for sp in lanes.enabled_lanes(s) if sp.estimator == lanes.LLM)
    if llm_lanes:
        marks = ",".join("?" for _ in llm_lanes)
        n = store.db.execute(f"SELECT COUNT(*) FROM rag_cases WHERE lane IN ({marks})", llm_lanes).fetchone()[0]
        out.append(f"lanes that ask the language model with these cases: {', '.join(llm_lanes)}; cases from those lanes: {n:,} of {cases:,}")
    avg = store.db.execute("SELECT AVG(LENGTH(body)) FROM rag_cases").fetchone()[0]
    out.append(f"average case text length: {float(avg or 0):.0f} characters (a case is the title, the prior estimate, how it resolved and a short note: thin context for similar-case lookup)")
    for spec in lanes.enabled_lanes(s):
        if spec.estimator in (lanes.LLM, lanes.SHAPE):
            r = store.db.execute("SELECT COUNT(*) AS n, SUM(CASE WHEN reason LIKE 'model error%' THEN 1 ELSE 0 END) AS err, AVG(ABS(model_p - ask)) AS gap FROM decisions WHERE lane = ? AND model_p IS NOT NULL", (spec.name,)).fetchone()
            if r["n"] or r["err"]:
                out.append(f"estimator {spec.estimator} on {spec.name}: {int(r['n'] or 0)} answers, {int(r['err'] or 0)} model errors, average |answer - ask| {float(r['gap'] or 0):.3f}")
    return out


def report(s: Settings, store: Any | None = None, root: Path | None = None) -> list[str]:
    root = root or repo_root()
    out = ["CODE AND CONFIGURATION AUDIT (candidates to check, not verdicts)", ""]
    if root is None:
        out.append("source tree not available here (the installed package only): code and settings checks skipped; run `uv run python -m kalshi_moonshot audit` in the repository")
    else:
        out += ["== SETTINGS AND VARIABLES (deprecation, unification)", *settings_audit(root, s)]
        out += ["", "== DEAD CODE", *dead_code(root)]
        out += ["", "== DUPLICATED CODE (DRY)", *duplication(root)]
        out += ["", "== MODULARITY", *size_report(root)]
        out += ["", "== TEST REACH", *test_reach(root)]
    if store is not None:
        out += ["", "== RAG AND LLM CONTEXT", *rag_health(store, s)]
    return out
