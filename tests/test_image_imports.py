"""The bot's source imports only what its image installs (requirements.lock).

2026-11-18 14:16 ET: pricevol.py imported numpy at module level. The host's image has no numpy, so importing the watcher failed, the exit watcher never started ("could not start the tape recorder; continuing without it") and live bets had no exits until the fix. The tests passed because the Mac's virtualenv has numpy."""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALIASES = {"yaml": "pyyaml"}  # import name -> distribution name
MAC_ONLY = {"studies.py": {"yaml"}}  # the studies command (run from the Mac, where the dev requirements are installed) imports yaml; the image has no yaml and nothing the bot runs imports this module


def installed() -> set[str]:
    out = set()
    for line in (ROOT / "requirements.lock").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "-", "--")) and not line[0].isspace():
            out.add(re.split(r"[=<>\[ ;]", line)[0].lower().replace("_", "-"))
    return out


def top_level_imports(path: Path) -> set[str]:
    names = set()
    for node in ast.parse(path.read_text()).body:  # module level only: an import inside a function runs only where that function is called
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_every_module_level_import_in_the_source_is_in_the_image():
    have = installed()
    assert "httpx" in have  # the list was read
    std = set(sys.stdlib_module_names)
    missing = {}
    for path in (ROOT / "src" / "kalshi_moonshot").rglob("*.py"):
        for name in top_level_imports(path):
            dist = ALIASES.get(name, name).lower().replace("_", "-")
            if name not in std and name != "kalshi_moonshot" and dist not in have and name not in MAC_ONLY.get(path.name, ()):
                missing.setdefault(name, []).append(path.name)
    assert not missing, f"imported at module level but not installed in the image: {missing}"


def test_the_time_aware_volatility_runs_without_numpy(monkeypatch):
    import importlib

    monkeypatch.setitem(sys.modules, "numpy", None)  # an import of it now raises ImportError, as on the host
    sys.modules.pop("kalshi_moonshot.pricevol", None)
    pv = importlib.import_module("kalshi_moonshot.pricevol")
    rows = [(float(t), 0.5 + 0.0004 * t) for t in range(0, 600)]
    seq = pv.grid(rows, 600.0)
    assert seq is not None and isinstance(seq, list)
    assert pv.static_rate(seq) is not None and pv.implied_rate(seq, 200.0) is not None
