"""Build tests/fixtures/wave_low_paths.json.gz: the 1 s spot path (an hour before the open and the window) of the two markets the user corrected on 2026-11-27, read from
the tape's index_ticks (ETH: ETHUSD_RTI, ETH 13:00 ET market, target 1659.16; SILVER: KP_SILVER, 11:45 ET market, target 43.329).
Usage: scripts/cloud.sh sql "COPY (SELECT ts, value FROM tape.index_ticks WHERE index_id='ETHUSD_RTI' AND ts BETWEEN <open-3600> AND <open+900> ORDER BY ts) TO STDOUT WITH CSV" > eth.csv
(same for silver), then: python scripts/wave_low_fixture.py eth.csv silver.csv"""
import csv
import gzip
import json
import sys

sys.path.insert(0, "src")
from kalshi_moonshot.wavetop import grid  # noqa: E402

MARKETS = {"eth-1300": ("ETHUSD_RTI", 1795801500, 1659.16), "silver-1145": ("KP_SILVER", 1795797000, 43.329)}


def main(eth_csv, silver_csv):
    out = []
    for (name, (index, opened, target)), path in zip(MARKETS.items(), (eth_csv, silver_csv), strict=True):
        rows = [(float(x[0]), float(x[1])) for x in csv.reader(open(path)) if len(x) == 2]
        T, V = [r[0] for r in rows], [r[1] for r in rows]
        g = grid(T, V, opened - 3600, opened + 900)
        out.append({"id": name, "index": index, "opened": opened, "target": target, "hour": g[:3600], "v": g[3600:]})
    with gzip.open("tests/fixtures/wave_low_paths.json.gz", "wt") as fh:
        json.dump(out, fh)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
