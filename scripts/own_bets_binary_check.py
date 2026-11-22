"""THE BINARY RISK LANES' OWN BETS: what the binary_risk step recorded at each buy, recomputed from the raw ticks, then winners against losers per reading (2026-11-21; user: "It's supposed to be comparing our wins and losses and the features inside our logic"; the method of studies/results/2026-11-21-own-bets-*.txt on the live commodity module, here on the lanes that buy on the binary risk model).
Window: settled paper bets of crypto_risk_score and crypto_binary_risk whose note carries a binary_risk step, from the step's first bet (2026-11-20) on. Each as it really ended (its own sale or settlement, fee on both legs).
RECORDED (from the step's note): the predicted net per $1 and which exit it is for, the top-5% cut, the blended cushion and its 1/3/5-minute parts, the 24 h drift, the 30-minute place.
RECOMPUTED: binaryrisk.readings on the settlement index's raw ticks against Kalshi's strike (windows.strike), with the spot 24 h and 72 h before the open as the drift references, at the second the step read: the latest second in the 20 before the row's stamp whose ask is the recorded ask.
Prints the biggest losers and winners with recorded and recomputed side by side, how often each reading recomputes, then, per lane, each reading by thirds: n, ahead, dollars. Writes own_bets_binary.json.gz (every bet, recorded and recomputed) to DATA_DIR.
Run: SPOT=0 scripts/cloud_job.sh scripts/own_bets_binary_check.py"""
import bisect, datetime, gzip, json, os, re, statistics
from pathlib import Path
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.kalshiws import index_for_series as crypto_index
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
T0 = datetime.datetime(2026, 11, 20, 5, tzinfo=datetime.timezone.utc).timestamp()
num = lambda pat, t: (float(m.group(1)) if (m := re.search(pat, t)) else None)
W = {w["ticker"]: w for w in st.execute("SELECT ticker, series, close_ts, strike, result FROM windows WHERE close_ts>?", (T0,)).fetchall()}
TK = {}


def ticks(ix, lo, hi):
    got = TK.get(ix)
    if got is None:
        r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, T0 - 6000)).fetchall()
        got = TK[ix] = ([float(x[0]) for x in r], [float(x[1]) for x in r])
    return got


rows = []
for r in st.execute("SELECT ts, ticker, lane, side, ask, reason, outcome, exit_price, exit_ts, exit_reason, contracts, stake_usd FROM decisions WHERE action='bet' AND stake_usd>0 AND contracts>0 AND ts>? AND outcome IN ('yes','no') AND lane IN ('crypto_risk_score','crypto_binary_risk') ORDER BY ts", (T0,)).fetchall():
    note, w = r["reason"] or "", W.get(r["ticker"])
    m = re.search(r"binary_risk yes \((binary risk .*?)\)\)", note)
    if not m or not w or not w["strike"]: continue
    part, side = m.group(1), r["side"]
    cz = re.search(r"cushion ([-+\d.]+) \(1/3/5 min ([-+\d.]+)/([-+\d.]+)/([-+\d.]+)", part)
    rec = {"pred": num(r"binary risk ([-+\d.]+)/\$1", part), "exit": (re.search(r"/\$1 by ([^(]+?) \(", part) or [0, "?"])[1], "cut": num(r"from ([-+\d.]+);", part), "czb": float(cz.group(1)) if cz else None, "cz1": float(cz.group(2)) if cz else None,
           "cz3": float(cz.group(3)) if cz else None, "cz5": float(cz.group(4)) if cz else None, "dr24": num(r"24 h drift ([-+\d.]+)", part), "pos30": num(r"30-min place ([\d.]+)", part), "ask": num(r"price yes \(ask ([\d.]+)", note) or float(r["ask"])}
    n, close = float(r["contracts"]), float(w["close_ts"])
    basis = float(r["stake_usd"]) / n
    sold = r["exit_price"] is not None and r["exit_ts"] is not None and float(r["exit_ts"]) < close - 1
    won = r["outcome"] == side
    per = ((float(r["exit_price"]) - fee(float(r["exit_price"]))) if sold else (1.0 if won else 0.0)) - basis
    ix = crypto_index(w["series"])
    T, V = ticks(ix, 0, 0)
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], r["ts"] - 60, r["ts"] + 1)).fetchall()
    now = float(r["ts"])
    if q:
        qt = [float(x[0]) for x in q]
        sc = 100.0 if max(float(x[2]) for x in q) > 1.5 else 1.0
        for d in range(21):
            j = bisect.bisect_right(qt, r["ts"] - d) - 1
            if j < 0: break
            a = float(q[j][2]) / sc if side == "yes" else 1.0 - float(q[j][1]) / sc
            if abs(a - rec["ask"]) < 0.006:
                now = r["ts"] - d; break
    opened = close - 900.0
    ref = []
    for h in (24.0, 72.0):
        k = bisect.bisect_right(T, opened - h * 3600.0) - 1
        ref.append(V[k] if k >= 0 and opened - h * 3600.0 - T[k] < 1800.0 else 0.0)
    f = br.readings(T, V, now, float(w["strike"]), close - now, (ref[0], ref[1]) if ref[0] > 0 and ref[1] > 0 else None)
    sg = 1.0 if side == "yes" else -1.0
    new = None
    if f is not None:
        new = {"czb": sg * (f["cz1"] + f["cz3"] + f["cz5"]) / 3.0, "cz1": sg * f["cz1"], "cz3": sg * f["cz3"], "cz5": sg * f["cz5"], "dr24": sg * f["dr24"], "pos30": f["pos30"] if side == "yes" else 1.0 - f["pos30"],
               "tr13": sg * f["tr13"], "mv30": sg * f["mv30"], "vr": f["vr"], "left": f["left"], "czt": sg * f["czt"]}
    rows.append({"ts": r["ts"], "ticker": r["ticker"], "lane": r["lane"], "side": side, "paid": basis, "net": per * n, "stake": float(r["stake_usd"]), "won": won, "sold": sold, "exit_price": r["exit_price"] if sold else None,
                 "exit_step": (re.search(r"sold at (\w+)\]", r["exit_reason"] or "") or [0, "sold"])[1] if sold else "settled", "rec": rec, "new": new, "read_before_stamp": r["ts"] - now, "minute": (now - opened) / 60.0, "drift_refs": ref[0] > 0})
with gzip.open(Path(os.environ.get("DATA_DIR", "/data")) / "own_bets_binary.json.gz", "wt") as fh: json.dump(rows, fh)

fmt = lambda v, p="%+.1f": (p % v) if v is not None else " n/a"
when = lambda t: datetime.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")
print(f"window: {when(rows[0]['ts'])} to {when(rows[-1]['ts'])} ET; {len(rows)} settled paper bets with a binary_risk note; recomputed on {sum(1 for x in rows if x['new'])}; with drift references {sum(1 for x in rows if x['drift_refs'])}")
for lane in ("crypto_risk_score", "crypto_binary_risk"):
    B = [x for x in rows if x["lane"] == lane]
    if not B: continue
    Wn, L = [x for x in B if x["net"] > 0], [x for x in B if x["net"] <= 0]
    print(f"\n################ {lane}: {len(B)} bets, {sum(x['net'] for x in B):+.2f}$, ahead {len(Wn)} (avg {statistics.mean(x['net'] for x in Wn):+.2f}), behind {len(L)} (avg {statistics.mean(x['net'] for x in L):+.2f}); ended: " + ", ".join(f"{k} {sum(1 for x in B if x['exit_step'] == k)}" for k in sorted({x['exit_step'] for x in B})))

    def show(title, g):
        print("\n  " + title + "\n  time ET     market                       side paid  min ended            net$ | pred  cut | cushion rec/new  1min rec/new  3min rec/new  5min rec/new | drift rec/new | place rec/new | trend  move30  vol")
        for x in g:
            a, b = x["rec"], x["new"] or {}
            print("  %s %-28s %-3s %.2f %4.1f %-10s %s %+7.2f | %s %s | %s/%s    %s/%s    %s/%s    %s/%s  | %s/%s    | %s/%s    | %s  %s  %s" % (when(x["ts"]), x["ticker"], x["side"], x["paid"], x["minute"], x["exit_step"], fmt(x["exit_price"], "%.2f") if x["sold"] else ("won " if x["won"] else "LOST"), x["net"],
                  fmt(a["pred"], "%+.2f"), fmt(a["cut"], "%+.2f"), fmt(a["czb"]), fmt(b.get("czb")), fmt(a["cz1"]), fmt(b.get("cz1")), fmt(a["cz3"]), fmt(b.get("cz3")), fmt(a["cz5"]), fmt(b.get("cz5")), fmt(a["dr24"]), fmt(b.get("dr24")), fmt(a["pos30"], "%.2f"), fmt(b.get("pos30"), "%.2f"), fmt(b.get("tr13")), fmt(b.get("mv30")), fmt(b.get("vr"), "%+.2f")))
    S = sorted(B, key=lambda x: x["net"])
    show("THE 12 BIGGEST LOSERS", S[:12]); show("THE 12 BIGGEST WINNERS", S[::-1][:12])
    print("\n  DOES EACH RECORDED READING RECOMPUTE? (the note rounds to 0.1, the place to 0.01)")
    for k, tol in (("czb", 0.1), ("cz1", 0.1), ("cz3", 0.1), ("cz5", 0.1), ("dr24", 0.1), ("pos30", 0.02)):
        pr = [(x["rec"][k], x["new"][k]) for x in B if x["new"] and x["rec"][k] is not None]
        d = sorted(abs(u - v) for u, v in pr)
        if d: print("    %-6s n %4d within %-4g: %4d (%2.0f%%)  median gap %.3f  90th pct %.3f  worst %.2f" % (k, len(d), tol, sum(1 for e in d if e <= tol + 1e-9), 100.0 * sum(1 for e in d if e <= tol + 1e-9) / len(d), d[len(d) // 2], d[int(len(d) * .9)], d[-1]))
    print("    the step read %s s before the row's stamp (median); wrong side of the strike by the recomputed 1-minute cushion: %d of %d" % (fmt(statistics.median(x["read_before_stamp"] for x in B), "%.0f"), sum(1 for x in B if x["new"] and x["new"]["cz1"] < 0), sum(1 for x in B if x["new"])))
    print("\n  WINNERS AGAINST LOSERS, each reading by thirds (range, n, ahead, dollars); recorded readings first, then the recomputed ones the note does not carry")
    feats = [("price paid", lambda x: x["paid"]), ("minute bought", lambda x: x["minute"]), ("predicted net (rec)", lambda x: x["rec"]["pred"]), ("pred over its cut (rec)", lambda x: None if x["rec"]["pred"] is None or x["rec"]["cut"] is None else x["rec"]["pred"] - x["rec"]["cut"]),
             ("cushion blend (rec)", lambda x: x["rec"]["czb"]), ("cushion 1 min (rec)", lambda x: x["rec"]["cz1"]), ("cushion 5 min (rec)", lambda x: x["rec"]["cz5"]), ("24 h drift (rec)", lambda x: x["rec"]["dr24"]), ("30-min place (rec)", lambda x: x["rec"]["pos30"]),
             ("trend 1v3 min (new)", lambda x: x["new"] and x["new"]["tr13"]), ("30-min move (new)", lambda x: x["new"] and x["new"]["mv30"]), ("volatility ratio (new)", lambda x: x["new"] and x["new"]["vr"])]
    for name, fn in feats:
        v = sorted(((fn(x), x["net"]) for x in B if fn(x) is not None), key=lambda t: t[0])
        if len(v) < 30: continue
        n3 = len(v)
        parts = [v[:n3 // 3], v[n3 // 3:2 * n3 // 3], v[2 * n3 // 3:]]
        print("    %-24s " % name + " | ".join("%6.2f..%6.2f n%4d %3.0f%% %+7.1f" % (g[0][0], g[-1][0], len(g), 100.0 * sum(1 for t in g if t[1] > 0) / len(g), sum(t[1] for t in g)) for g in parts))
    for name, fn in (("exit the prediction is for", lambda x: x["rec"]["exit"]), ("side", lambda x: x["side"])):
        ks = sorted({fn(x) for x in B})
        print("    %-24s " % name + "; ".join("%s n%d ahead %.0f%% %+.1f$" % (k, sum(1 for x in B if fn(x) == k), 100.0 * sum(1 for x in B if fn(x) == k and x["net"] > 0) / max(1, sum(1 for x in B if fn(x) == k)), sum(x["net"] for x in B if fn(x) == k)) for k in ks))
