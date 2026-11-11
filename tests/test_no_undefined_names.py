"""No module may use a name it never imported or defined (2026-11-11: watcher.py called perpref.index_for_series without importing perpref; `import watcher` and the whole suite passed, and the start-up warm-up failed with a
NameError on the live bot for fifteen minutes). Ruff finds this class of bug without running the code."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_no_undefined_names_in_the_package():
    root = Path(__file__).resolve().parents[1]
    cmd = [sys.executable, "-m", "ruff", "check", str(root / "src"), "--select", "F821,F822,F823", "--output-format", "concise", "--no-cache"]
    if shutil.which("ruff") is None and subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True).returncode != 0:
        pytest.skip("ruff is not installed")
    out = subprocess.run(cmd, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
