"""ROW DIGEST FROM THE CACHE (2026-11-21): the same numbers scripts/study_cache_digest.py prints on the runner from the database, printed from the study cache, to compare. Run: .venv/bin/python scripts/study_cache_digest_local.py job-results/<cache id>"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import study_cache as sc
from kalshi_moonshot import binaryrisk as br
S = sc.side_rows(sc.Cache(sys.argv[1]), kind="commodity")
def digest(S):
    cm = [r for r in S if r[9] == "commodity"]
    out = {"n": len(cm), "ask": sum(r[3] for r in cm), "won": sum(r[7] for r in cm), "left": sum(r[6] for r in cm), "nets": [sum(r[2][e] for r in cm) for e in range(5)], "vec": [sum(r[1][i] for r in cm) for i in range(len(br.NAMES))]}
    ks = sorted({(r[4], r[5]) for r in cm})[::400]
    out["samples"] = {f"{k[0]}|{k[1]}": [[round(r[0], 3), round(r[3], 6), round(r[2][0], 6), round(r[1][2], 6), round(r[1][14], 6)] for r in cm if (r[4], r[5]) == k][:3] for k in ks}
    return out
print("DIGEST " + json.dumps(digest(S)))
