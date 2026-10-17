"""Commands: scan | run | resolve | report | balance | kill | unkill | doctor"""

from __future__ import annotations

import logging
import sys
import time

from . import config, report, runner
from .kalshi import KalshiClient, KalshiError
from .llm import Ollama
from .store import Store

log = logging.getLogger("moonshot")


def _client(s: config.Settings) -> KalshiClient:
    return KalshiClient(s.base_url, s.key_id, s.private_key_path)


def _print_scan(results: list[runner.Outcome]) -> None:
    for r in results:
        p = f"{r.model_p:.3f}" if r.model_p is not None else "  -  "
        e = f"{r.edge:+.3f}" if r.edge is not None else "  -  "
        print(f"{r.action:4} ask={r.ask:.2f} model={p} edge={e}  {r.ticker}\n     {r.title[:90]} | {r.note[:110]}")


def cmd_scan(s: config.Settings) -> int:
    est = Ollama(s.ollama_url, s.model)
    if not est.ping():
        print(f"Ollama is not reachable at {s.ollama_url}. Start it, or run `doctor`.")
        return 1
    results = runner.scan(s, _client(s), est, Store(s.db_path))
    _print_scan(results)
    print(f"\n{len(results)} evaluated, mode={s.mode}, env={s.env}")
    return 0


def cmd_run(s: config.Settings) -> int:
    store, client = Store(s.db_path), _client(s)
    est = Ollama(s.ollama_url, s.model)
    log.info("loop start: env=%s mode=%s real_money=%s", s.env, s.mode, s.real_money)
    while True:
        try:
            if est.ping():
                _print_scan(runner.scan(s, client, est, store))
            else:
                log.warning("Ollama not reachable at %s; skipping this pass", s.ollama_url)
            log.info("resolved %d settled markets", runner.resolve(client, store))
        except Exception:  # keep the loop alive; the next pass retries
            log.exception("pass failed")
        time.sleep(s.scan_interval_minutes * 60)


def cmd_resolve(s: config.Settings) -> int:
    print(f"resolved {runner.resolve(_client(s), Store(s.db_path))} markets")
    return 0


def cmd_report(s: config.Settings) -> int:
    summary = report.summarize(Store(s.db_path).settled_decisions())
    for k, v in summary.items():
        print(f"{k:28} {v:.4f}" if isinstance(v, float) else f"{k:28} {v}")
    if summary["settled"] < 30:
        print("\nFewer than 30 settled decisions: too early to say whether there is an edge.")
    return 0


def cmd_balance(s: config.Settings) -> int:
    print(_client(s).balance())
    return 0


def cmd_kill(s: config.Settings) -> int:
    s.kill_file.write_text("stop\n")
    print(f"kill switch on ({s.kill_file}). No orders will be placed until `unkill`.")
    return 0


def cmd_unkill(s: config.Settings) -> int:
    s.kill_file.unlink(missing_ok=True)
    print("kill switch off")
    return 0


def cmd_doctor(s: config.Settings) -> int:
    print(f"env={s.env} mode={s.mode} real_money={s.real_money} base={s.base_url}")
    c = _client(s)
    try:
        n = sum(1 for _ in c.open_markets(pages=1))
        print(f"public market data: ok ({n} markets on page 1)")
    except KalshiError as exc:
        print(f"public market data: FAILED {exc}")
    if c.authed:
        try:
            print(f"balance: {c.balance()}")
        except KalshiError as exc:
            print(f"signed call: FAILED {exc}")
    else:
        print("credentials: not set (fine for observe mode)")
    o = Ollama(s.ollama_url, s.model)
    print(f"ollama {s.ollama_url} model={s.model}: {'ok' if o.ping() else 'NOT REACHABLE'}")
    print(f"kill switch: {'ON' if s.kill_file.exists() else 'off'}")
    return 0


COMMANDS = {
    "scan": cmd_scan, "run": cmd_run, "resolve": cmd_resolve, "report": cmd_report,
    "balance": cmd_balance, "kill": cmd_kill, "unkill": cmd_unkill, "doctor": cmd_doctor,
}


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print(__doc__)
        return 2
    return COMMANDS[argv[1]](config.load())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
