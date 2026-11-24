"""Does taking a profit early beat the PriskP exit on early buys? (2026-11-24; user, on the 10:45 ET wave where seven live buys each saw a bid of 0.91-0.97 within a minute and then lost: "buys this early need to exit on profit"; "they all just hit a profit and should have sold")
QUESTION (one, on the same buys): on every PriskP model buy made before minute 5 of the window (priskp_run.py rule "model", held to settlement unless sold; the file's bids every 10 s after the buy), does selling the first time the bid pays a profit beat (a) holding and (b) the cross-only exit the lane runs (sell at the bid once the index's 60 s average has crossed to the wrong side of the strike)? Fee 0.07 p (1-p) on every leg; a sale is at the recorded bid of that 10 s mark, no fill modelled.
RULES (stated before the run): TAKE G: sell the first time the bid after its sale fee is G x u above the price paid plus its buy fee, u = the bid's typical minute from the quotes before the buy (the root-mean-square 10 s change of the side's mid x sqrt(6); the chart's quote marks, so only buys with at least 12 marks before them). G = 0 (any profit after both fees), 0.5 and 1.0, all shown, none chosen. After a take is not made, the cross-only exit runs as it does now. "Early" = before minute 5 (my choice, said so; the user said "this early"). Shown beside: the same on buys from minute 5 on, for contrast.
READ: per rule, n, net per $1 staked after fees (every bet equal), the bets it beat and lost to holding, the sales of bets that went on to win (a whipsaw), the net on the bets that lost when held, split into thirds of u (the bid's own typical minute; no asset). A lead only: a day-by-day split is printed; the file covers 11-09 to 11-23, the logic before Monday 11-09 is not counted.
Run: .venv/bin/python scripts/priskp_early_take_replay.py PATH/priskp_buys_crypto.json.gz"""
import gzip
import json
import math
import sys

rows = json.loads(gzip.decompress(open(sys.argv[1], "rb").read()))
GS = (0.0, 0.5, 1.0)


def fee(p):
    return 0.07 * p * (1.0 - p)


def prep(r):
    px = (r.get("chart") or {}).get("px")
    if not px or r.get("bids") is None or r.get("cross") is None:
        return None
    mids = [(a[0] + a[1]) / 2.0 if a else None for a in px]
    k0 = int(r["minute"] * 6)  # the 10 s mark of the buy
    pre = [m for m in mids[: k0 + 1] if m is not None]
    if len(pre) < 12:
        return None
    ch = [b - a for a, b in zip(pre, pre[1:], strict=False)]
    u = math.sqrt(sum(c * c for c in ch) / len(ch)) * math.sqrt(6.0)
    if u <= 0:
        return None
    p = r["price"]
    cost = p + fee(p)
    held = ((1.0 if r["won"] else 0.0) - cost) / cost
    path = list(zip(r["bids"], r["cross"], strict=False))

    def run(g):
        for bid, cr in path:
            if bid is None:
                continue
            if g is not None and bid - fee(bid) - cost >= g * u:
                return (bid - fee(bid) - cost) / cost, "take"
            if cr is not None and cr <= 0:
                return (bid - fee(bid) - cost) / cost, "cross"
        return held, "held"

    out = {"held": held, "u": u, "won": r["won"], "minute": r["minute"], "day": int(r["close"] // 86400)}
    out["cross"], _ = run(None)
    for g in GS:
        out[f"take{g}"], out[f"how{g}"] = run(g)
    return out


res = [x for x in (prep(r) for r in rows if r["rule"] == "model") if x]
print(f"{len(res)} model buys with a quote history and a bid path (of {sum(1 for r in rows if r['rule'] == 'model')})")


def report(lab, g):
    if not g:
        return
    n = len(g)
    print(f"\n{lab}: {n} buys; held {sum(x['held'] for x in g) / n:+.4f} per $1")
    print(f"{'rule':10s} {'net/$1':>8s} {'beat':>5s} {'lost':>5s} {'whipsaw':>8s} {'sold':>5s} {'on held-losers':>15s}")
    for key, hk in [("cross", None)] + [(f"take{g_}", f"how{g_}") for g_ in GS]:
        net = sum(x[key] for x in g) / n
        beat = sum(1 for x in g if x[key] > x["held"] + 1e-9)
        lost = sum(1 for x in g if x[key] < x["held"] - 1e-9)
        sold = [x for x in g if (x[hk] == "take" if hk else x["cross"] != x["held"])]
        whip = sum(1 for x in sold if x["won"])
        hl = [x for x in g if x["held"] < 0]
        print(f"{key:10s} {net:+8.4f} {beat:5d} {lost:5d} {whip:8d} {len(sold):5d} {sum(x[key] for x in hl) / max(1, len(hl)):+15.3f} (held {sum(x['held'] for x in hl) / max(1, len(hl)):+.3f}, n {len(hl)})")


early = [x for x in res if x["minute"] < 5]
report("EARLY (before minute 5)", early)
report("LATER (minute 5 on)", [x for x in res if x["minute"] >= 5])
us = sorted(x["u"] for x in early)
cuts = [us[len(us) // 3], us[2 * len(us) // 3]]
for k, name in enumerate(("small u", "middle u", "large u")):
    report(f"EARLY, {name}", [x for x in early if sum(x["u"] > c for c in cuts) == k])
print("\nEARLY by day (net per $1: held / cross / take0.5):")
for d in sorted({x["day"] for x in early}):
    g = [x for x in early if x["day"] == d]
    n = len(g)
    print(f"  day {d} n {n:4d}  {sum(x['held'] for x in g) / n:+.3f} / {sum(x['cross'] for x in g) / n:+.3f} / {sum(x['take0.5'] for x in g) / n:+.3f}")
