"""Would a give-back sale have helped the live PriskP bets? (2026-11-24; user: "this doesn't seem to take profit when it's a dip"; on KXBNB/SOL/XRP/HYPE 22:00-22:15 the bet was in profit for under a minute, then fell to a loss and was sold there.)
QUESTION (one): on every live priskp bet since 2026-11-24 05:00 UTC, replay the quote tape from the buy: the bet's net = bid - sale fee - (price paid + buy fee); once the best net so far reaches one wobble (the bid's typical 10 s move: its 1 s moves' root-mean-square x sqrt(10)), sell at the first quote where net is still over zero but has given back GIVE of the best net. Compare, bet by bet and in dollars, with what the bet actually made (its sale, or settlement if it was held).
HONEST LIMITS: GIVE = 0.5 is my choice, not scored; the wobble uses the bid's own moves over the bet's whole life (small look-ahead); a sale made earlier by the rule is scored at the bid of that quote (the live sell would take the bid at its own tick); the rule ignores the lane's other exit steps (they could have sold earlier). n is one evening's bets. A lead only.
Run on the host: scripts/cloud.sh run scripts/priskp_giveback_replay.py"""
import datetime
import functools
import traceback
import json
import math
import sys
import urllib.request

from kalshi_moonshot import config, db

GIVE = float(sys.argv[1]) if len(sys.argv) > 1 else 0.5
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
tot_act = tot_rule = 0.0
print = functools.partial(print, flush=True)
print(f"give-back share {GIVE}; {len(rows)} bets")
for r in rows:
    try:
      tk, side, n = r["ticker"], r["side"], float(r["contracts"])
      pay = float(r["stake_usd"]) / n
      cost = pay + fee(pay)
      close = (int(r["ts"]) // 900 + 1) * 900.0
      end = float(r["exit_ts"]) if r["exit_ts"] else close
      q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts", (tk, r["ts"], end)).fetchall()
      if len(q) < 30:
          print(tk, "no quotes")
          continue
      b = [float(x["bid"]) if side == "yes" else 1.0 - float(x["ask"]) for x in q]
      t = [float(x["ts"]) for x in q]
      d = [b[i + 1] - b[i] for i in range(len(b) - 1) if t[i + 1] - t[i] <= 1.5]
      wob = math.sqrt(sum(x * x for x in d) / max(len(d), 1)) * math.sqrt(10.0) if d else 0.05
      if r["exit_ts"]:
          act = float(r["exit_price"]) - fee(float(r["exit_price"])) - cost
      else:
          m = market(tk)
          res = m.get("result")
          act = ((1.0 if res == side else 0.0) - cost) if res else 0.0
      best = 0.0
      out = None
      for i in range(len(b)):
          net = b[i] - fee(b[i]) - cost
          best = max(best, net)
          if best >= wob and 0 < net <= (1.0 - GIVE) * best:
              out = (t[i] - r["ts"], b[i], net)
              break
      rule = out[2] if out else act
      tot_act += act * n
      tot_rule += rule * n
      print(f"{tk[:24]:24s} {side:3s} paid {pay:.2f} wobble {wob:.3f} actual {act:+.3f}" + (f"  rule sells +{out[0]:.0f}s at {out[1]:.2f} net {out[2]:+.3f}" if out else "  rule: no sale"))
    except Exception:
      print(r["ticker"], "failed", traceback.format_exc()[-300:])
print(f"\nactual ${tot_act:+.2f}; with the give-back sale ${tot_rule:+.2f}")
