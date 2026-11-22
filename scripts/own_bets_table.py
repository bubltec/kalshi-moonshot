"""The bot's own live commodity bets that carry the buy module's step notes, one row a bet: what each step read at the buy and how the bet really ended. Reads job-results/*/files/own_bets.json.gz (scripts/own_bets_dump.py). No replay, no new reading.
    python3 scripts/own_bets_table.py [LANEPREFIX=commodity] [MODE=trade]"""
import datetime, glob, gzip, json, re, sys

ET = datetime.timezone(datetime.timedelta(hours=-5))
arg = dict(a.split("=") for a in sys.argv[1:])
R = json.load(gzip.open(sorted(glob.glob("job-results/*/files/own_bets.json.gz"))[-1]))
num = lambda pat, s, d=None: (float(m.group(1)) if (m := re.search(pat, s)) else d)


def parse(r):
    s = r["reason"] or ""
    m = re.search(r"\[chain (\w+): ([^|]+)\|(.*?)\](?: \[|$)", s)
    if not m: return None
    part = next((p for p in m.group(3).split(" | ") if p.strip().startswith(r["side"].upper() + " passed")), None)
    if part is None: return None
    f = r["factors"] or {}
    return {"module": m.group(1), "ask": num(r"price yes \(ask ([\d.]+)", part), "vol": num(r"volatility ([\d.]+)x", part), "variety": (re.search(r"variety yes \(variety (\w)", part) or [None, None])[1],
            "cushion_z": num(r"cushion z ([-+\d.]+)", part), "crossings": num(r"(\d+) crossings", part), "perp_chance": num(r"perp's chance ([\d.]+)", part), "perp_less_ask": num(r"against the ask [\d.]+ \(([-+\d.]+)", part),
            "trend_c": num(r"price moved ([-+\d.]+)c", part), "spike": (re.search(r"spike yes \(([^)]*)\)", part) or [None, "?"])[1], "center": (re.search(r"center_agree yes \(([^)]*)\)", part) or [None, None])[1],
            "same_side": num(r"same_side yes \((\d+) of", part), "ctx_wins": num(r"context score \(shadow\): wins ([\d.]+)", s), "ctx_short": num(r"shortfall ([-+\d.]+)", s), "ctx_pct": num(r"(\d+)(?:st|nd|rd|th) percentile", s),
            "spike_z": num(r"spike view z ([-+\d.]+)", s), "minute": f.get("elapsed_min"), "spread": f.get("spread"), "basis_pct": num(r"\(basis ([-+\d.]+)%\)", s), "p_yes": num(r"P\(yes\) ([\d.]+)", s)}


B = []
for r in R:
    if r["mode"] != arg.get("MODE", "trade") or not r["lane"].startswith(arg.get("LANEPREFIX", "commodity")): continue
    p = parse(r)
    if p: B.append((r, p))
if __name__ == "__main__":
    print(f"{len(B)} bets with step notes, {datetime.datetime.fromtimestamp(B[0][0]['ts'], ET):%m-%d %H:%M} to {datetime.datetime.fromtimestamp(B[-1][0]['ts'], ET):%m-%d %H:%M} ET; total {sum(r['net'] for r, _ in B):+.2f}$, ahead {sum(1 for r, _ in B if r['net'] > 0)}, behind {sum(1 for r, _ in B if r['net'] <= 0)}")
    hdr = "time ET     market                        side paid  min  ended           net$  | vol  var cush cross perp  perp-ask trend spikez same ctx%  center"
    def show(title, g):
        print("\n" + title + "\n" + hdr)
        for r, p in g:
            end = "settled " + ("won " if r["won"] else "LOST") if not r["sold"] else "%-10s %.2f" % ((re.search(r"sold at (\w+)\]", r["exit_reason"] or "") or [0, "sold"])[1], r["exit_price"])
            f = lambda v, fmt: (fmt % v) if v is not None else "  -- "
            print("%s %-29s %-3s  %.2f %4s  %-15s %+6.2f | %s  %s  %s %s  %s  %s %s %s  %s  %s  %s" % (datetime.datetime.fromtimestamp(r["ts"], ET).strftime("%m-%d %H:%M"), r["ticker"], r["side"], r["paid"], f(p["minute"], "%4.1f"), end, r["net"],
                  f(p["vol"], "%.2f"), p["variety"] or "-", f(p["cushion_z"], "%+5.2f"), f(p["crossings"], "%3.0f"), f(p["perp_chance"], "%.3f"), f(p["perp_less_ask"], "%+.3f"), f(p["trend_c"], "%+5.1f"), f(p["spike_z"], "%+5.1f"), f(p["same_side"], "%1.0f"), f(p["ctx_pct"], "%3.0f"),
                  "agree" if p["center"] and "agree" in p["center"] else (p["center"] or "(no step)")[:28]))
    S = sorted(B, key=lambda x: x[0]["net"])
    show("THE 20 BIGGEST LOSERS", S[:20]); show("THE 20 BIGGEST WINNERS", S[::-1][:20]); show("EVERY BET, in time order", B)
