"""How long do Kalshi REST calls take from here? Read calls only; places and cancels nothing.

Times N calls each of a single-market read and an order-book read (and, with BALANCE=1, an authenticated balance read), back to back with a short gap, using the
bot's own client with its request pacing switched off for the test (so the numbers are the round trip, not the wait for a turn).
Run it where the bot runs to see what the bot sees:
    scripts/cloud.sh run scripts/api_latency_study.py [N=25]                       # the cloud host
    docker exec -i moonshot-local-bot-1 python - < scripts/api_latency_study.py    # this Mac

Order placement is not measured: that would put a real order on the exchange.
"""
import os
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshi import KalshiClient

s = config.reload()
N = int(os.environ.get("N", "25"))
client = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
client._gap = 0.0  # no pacing: measure the round trip itself

tape = db.connect(s.tape_path, "tape", shared=True)
row = tape.execute("SELECT ticker FROM quotes WHERE ticker LIKE '%15M%' ORDER BY ts DESC LIMIT 1").fetchone()
ticker = row[0]


def timed(fn):
    out = []
    for _ in range(N):
        t0 = time.perf_counter()
        try:
            fn()
        except Exception as exc:  # a failed call is a result too: say so, do not hide it
            out.append(None)
            err = type(exc).__name__
            continue
        out.append((time.perf_counter() - t0) * 1000.0)
        time.sleep(0.15)
    return out


def line(name, xs):
    ok = sorted(x for x in xs if x is not None)
    if not ok:
        return f"{name:<22} all {len(xs)} calls failed"
    q = lambda p: ok[min(len(ok) - 1, int(p * len(ok)))]  # noqa: E731
    return f"{name:<22} p50 {q(0.5):>5.0f} ms  p90 {q(0.9):>5.0f} ms  max {ok[-1]:>5.0f} ms  ({len(ok)}/{len(xs)} ok)"


client.market(ticker)  # the first call pays for the connection: not counted
print(f"{s.env} {s.base_url.split('//')[1].split('/')[0]}, {ticker}, {N} calls each")
print(line("GET market", timed(lambda: client.market(ticker))))
print(line("GET order book", timed(lambda: client.orderbook(ticker))))
if os.environ.get("BALANCE") == "1":  # off by default: /portfolio/balance is refused with 429 at a few calls a second, so this spends the live account's budget
    print(line("GET balance (signed)", timed(lambda: client.balance())))
