"""Two board readings against the bet: the hour's trend and the previous 15 minutes against this market's target. Would "do not buy against both" have helped? (2026-11-24; user, on the 22:15 NO bets: "the hour view on all those markets were trending up and the 15min previous either showed it level at the current target or under it. That should be a factor on the board. The hour trend and 15min comparison to current target. Don't buy against both.")
QUESTION (one): on every live priskp bet since 2026-11-24 05:00 UTC (as they ran: sold or settled), two readings at the buy, both signed so that positive is with the bet's side (a rise is with YES, a fall with NO):
  hour  = the spot's log change over the hour before the buy, in the spot's typical minutes (unit over the hour) -- against when negative;
  prior = the previous 15 minutes (the window before this market opened): its mean spot against this market's target (the strike), in the same units, positive when that window sat on the bet's side of the target -- against when negative.
A bet is "against both" when both are negative. Report n, net dollars and loss share by the four cells, the two single readings, and what the lane would have made leaving out the against-both bets. Also each 22:00 and 22:15 bet's two readings.
HONEST LIMITS: one day's bets (105) in one regime; signs only, no cutoff chosen, but the pairing of the two readings was fixed after the user's description, not before the data; 'as they ran' mixes the lane's exits with the entry; the bets left out are not replaced by others. A lead only; a rule on it goes to shadow first.
Run on the host: scripts/cloud.sh run scripts/priskp_hour_prior15_study.py"""
import datetime
import functools
import json
import math
import re
import traceback
import urllib.request

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import index_for_series

print = functools.partial(print, flush=True)
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)


def fee(p):
    return 0.07 * p * (1.0 - p)


def market(t):
    with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{t}", timeout=20) as r:
        return json.load(r)["market"]


rows = st.execute(
    "SELECT ticker, side, ts, stake_usd, contracts, exit_ts, exit_price FROM decisions WHERE lane='priskp' AND action='bet' AND mode='trade' AND ts > ? ORDER BY ts",
    (datetime.datetime(2026, 11, 24, 5, tzinfo=datetime.timezone.utc).timestamp(),),
).fetchall()
out = []
for r in rows:
    try:
        tk, side, n = r["ticker"], r["side"], float(r["contracts"])
        pay = float(r["stake_usd"]) / n
        cost = pay + fee(pay)
        m = market(tk)
        strike = float(m.get("floor_strike") or 0)
        if r["exit_ts"]:
            net = float(r["exit_price"]) - fee(float(r["exit_price"])) - cost
        else:
            res = m.get("result")
            if not res:
                continue
            net = (1.0 if res == side else 0.0) - cost
        ser = re.match(r"(KX[A-Z0-9]+15M)", tk).group(1)
        ix = index_for_series(ser)
        opened = (int(r["ts"]) // 900) * 900.0
        ticks_ = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, r["ts"] - 3600.0, r["ts"])).fetchall()
        if len(ticks_) < 1000 or strike <= 0:
            continue
        T = [float(x[0]) for x in ticks_]
        V = [float(x[1]) for x in ticks_]
        rets = [math.log(V[i + 1] / V[i]) for i in range(len(V) - 1) if T[i + 1] - T[i] <= 1.5 and V[i] > 0]
        unit = math.sqrt(sum(x * x for x in rets) / len(rets)) * math.sqrt(60.0)  # one typical minute
        sign = 1.0 if side == "yes" else -1.0
        hour = sign * math.log(V[-1] / V[0]) / (unit * math.sqrt(60.0))  # the hour's change in typical hours of a random walk
        pv = [V[i] for i in range(len(V)) if opened - 900.0 <= T[i] < opened]
        if len(pv) < 100:
            continue
        prior = sign * math.log((sum(pv) / len(pv)) / strike) / (unit * math.sqrt(15.0))  # mean of the window before open against the target, in typical 15 minutes
        out.append((tk, side, pay, hour, prior, net, n))
    except Exception:
        print(r["ticker"], "failed", traceback.format_exc()[-200:])
print(f"{len(out)} bets with a result and an hour of spot history")


def line(label, g):
    if not g:
        print(f"{label:46s} n   0")
        return
    tot = sum(x[5] * x[6] for x in g)
    lost = sum(1 for x in g if x[5] < 0)
    print(f"{label:46s} n {len(g):3d}  net ${tot:+8.2f}  lost {lost:3d} ({100.0 * lost / len(g):.0f}%)  per contract {sum(x[5] for x in g) / len(g):+.3f}")


line("all", out)
line("hour with the side", [x for x in out if x[3] >= 0])
line("hour against the side", [x for x in out if x[3] < 0])
line("prior 15 on the side's side of the target", [x for x in out if x[4] >= 0])
line("prior 15 against the side (wrong side of target)", [x for x in out if x[4] < 0])
line("both with the side", [x for x in out if x[3] >= 0 and x[4] >= 0])
line("hour against only", [x for x in out if x[3] < 0 and x[4] >= 0])
line("prior against only", [x for x in out if x[3] >= 0 and x[4] < 0])
both = [x for x in out if x[3] < 0 and x[4] < 0]
line("AGAINST BOTH", both)
keep = [x for x in out if not (x[3] < 0 and x[4] < 0)]
line("all bets but against-both", keep)
print("\nthe 22:00 and 22:15 bets (hour in typical hours, prior in typical 15 minutes, positive = with the bet):")
for x in sorted(out, key=lambda x: x[0]):
    if "2200" in x[0] or "2215" in x[0]:
        print(f"{x[0][:24]:24s} {x[1]:3s} paid {x[2]:.2f} hour {x[3]:+6.2f} prior15 {x[4]:+6.2f} net {x[5]:+.3f}{'  AGAINST BOTH' if x[3] < 0 and x[4] < 0 else ''}")

# (added after the first run, which found the sign of 'prior' is with the side on 102 of 105 bets: the user's "level at the target" is read as the prior window sitting close to the target, so prior's own size is cut at its quartiles; a cut chosen after seeing the first result)
srt = sorted(out, key=lambda x: x[4])
qn = len(srt) // 4
print("\nprior 15 by quartile of its own size (lowest = the previous window sat closest to or past the target)")
for k in range(4):
    line(f"  quartile {k + 1}", srt[k * qn:(k + 1) * qn if k < 3 else len(srt)])
low_cut = srt[qn - 1][4]
lowq = [x for x in out if x[4] <= low_cut]
line("prior in the lowest quarter AND hour against", [x for x in lowq if x[3] < 0])
line("prior in the lowest quarter AND hour with", [x for x in lowq if x[3] >= 0])
line("all bets but (lowest-quarter prior AND hour against)", [x for x in out if not (x[4] <= low_cut and x[3] < 0)])
