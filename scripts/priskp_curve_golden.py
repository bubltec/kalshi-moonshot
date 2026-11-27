"""The golden record of PriskP's curve reading on one real market (2026-11-27; user: "the two lanes must not share logic that one can change under the other").
PriskP's curve entry reads the spot through wavefacts.curve / wavefacts.leg, and those call into waveturn.py, the wave lane's reader. tests/test_lane_separation.py replays this market (ZEC 10:30 on 2026-11-27, the spot from the tape,
tests/fixtures/zec_1030_path.json.gz) through both functions, both sides, every 40 s of the window, and compares with tests/fixtures/priskp_curve_golden_zec.json. A change to the wave lane's reader that moves PriskP's reading fails the build.
REGENERATE ONLY when PriskP's own curve reading is changed ON PURPOSE (the PriskP session's change to wavefacts.py or priskp.py), and say so in the commit:   python3 scripts/priskp_curve_golden.py
Rebuild the path fixture from the host with scripts/cloud.sh sql (tape.index_ticks for ZECUSD_RTI, 09:13 to 10:31 ET) if it is ever lost."""
import datetime as dt
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from kalshi_moonshot import ticks as ticks_mod
from kalshi_moonshot import wavefacts

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
INDEX, TARGET = "ZECUSD_RTI", 729.0528
OPEN = float(int(dt.datetime(2026, 11, 27, 10, 15, tzinfo=dt.timezone(dt.timedelta(hours=-5))).timestamp()))


def run() -> list:
    with gzip.open(FIX / "zec_1030_path.json.gz", "rt") as fh:
        rows = json.load(fh)
    ticks_mod.history.clear()
    ticks_mod.history.warm(INDEX, [(float(t), float(v)) for t, v in rows], float(rows[0][0]) - 1.0)
    out = []
    for t in range(60, 880, 40):
        for side in ("yes", "no"):
            c = wavefacts.curve(INDEX, OPEN, OPEN + t + 0.5, TARGET, side, 900.0 - t - 0.5)
            leg = wavefacts.leg(INDEX, OPEN, OPEN + t + 0.5, TARGET, side, 900.0 - t - 0.5)
            out.append([t, side, c, leg])
    return json.loads(json.dumps(out, sort_keys=True, default=str))


if __name__ == "__main__":
    (FIX / "priskp_curve_golden_zec.json").write_text(json.dumps(run(), sort_keys=True, indent=0))
    print("written", len(run()), "looks")
