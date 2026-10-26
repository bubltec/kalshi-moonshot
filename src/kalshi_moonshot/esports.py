"""Does the local model, given news, price a CS2 or Dota 2 match better than Kalshi does? Measured, not traded.

For each match market about an hour before the listed start, the model is asked who wins from the team names and a news search (never the market's price),
and its answer is saved next to the market's bid and ask at that moment (`esports_forecasts`). When the match settles the outcome is read back.
`python -m kalshi_moonshot esports` then scores both with the Brier score (lower is better) on matches whose book was tight enough for the price to mean
something, and says whether the model has beaten the market by more than the noise.

It runs on its own thread, so a slow model call never delays a trading job, and it places no orders: nothing here can buy or sell.
The bar is set before looking: no esports lane is worth building unless the model beats the market over at least 100 settled matches by more than twice the
standard error of the difference. Each match gets two forecasts, so the effect of rankings can be read on its own: one from the news alone, and one from the news plus both teams'
rankings (rankings.py: Valve's standings for CS2, OpenDota's Elo for Dota 2). Both are scored against the same market price on the same matches.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .kalshi import KalshiError

log = logging.getLogger("moonshot")

GAMES = {"KXCS2GAME": "CS2", "KXDOTA2GAME": "Dota 2"}
FORECAST_WITHIN_MIN = 90.0  # ask once a match is this close to its listed start ...
MIN_MINUTES = 10.0  # ... and has not begun (a live match is priced from the score, not from news)
PER_PASS = 3  # model calls per pass: one thread, and each call takes seconds
EVERY = 600.0  # seconds between passes
TIGHT = 0.06  # a book wider than this has no price worth scoring against
SETTLE_AFTER_H = 6.0  # look for a result this long after the listed start
MIN_MATCHES, MIN_Z = 100, 2.0  # the bar for building a lane
MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
NEW_YORK = ZoneInfo("America/New_York")


def start_of(ticker: str) -> float | None:
    """The listed start from a ticker such as KXCS2GAME-26OCT241400G2AOT-OT (24 Oct 2026, 14:00 New York time)."""
    m = re.match(r"[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)", ticker)
    if not m or m[2] not in MONTHS:
        return None
    return datetime(2000 + int(m[1]), MONTHS[m[2]], int(m[3]), int(m[4]), int(m[5]), tzinfo=NEW_YORK).timestamp()


def team_of(market: dict[str, Any]) -> str:
    return re.sub(r"\s+wins\??$", "", (market.get("title") or "").strip(), flags=re.IGNORECASE) or (market.get("yes_sub_title") or "").strip()


def pending(client: Any, now: float) -> list[tuple[str, dict, str, str]]:
    """Matches about to start: (game, the market asked about, its team, the other team). One market per match."""
    out = []
    for prefix, game in GAMES.items():
        by_event: dict[str, list[dict]] = {}
        for m in client.series_markets(prefix):
            by_event.setdefault(m.get("event_ticker") or m["ticker"].rsplit("-", 1)[0], []).append(m)
        for markets in by_event.values():
            markets.sort(key=lambda m: m["ticker"])
            first = markets[0]
            start = start_of(first["ticker"])
            if start is None or not (MIN_MINUTES * 60 <= start - now <= FORECAST_WITHIN_MIN * 60):
                continue
            rival = next((team_of(m) for m in markets[1:]), "") or "their opponent"
            out.append((game, first, team_of(first), rival))
    return out


def _price(m: dict[str, Any], key: str) -> float | None:
    value = m.get(key)
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def run_once(client: Any, model: Any, searcher: Any, store: Any, now: float | None = None, rankings: Any = None) -> int:
    """Forecast up to PER_PASS matches that have not been forecast yet. Returns how many were saved."""
    now = time.time() if now is None else now
    saved = 0
    for game, m, team, rival in pending(client, now):
        if saved >= PER_PASS:
            break
        event = m.get("event_ticker") or m["ticker"].rsplit("-", 1)[0]
        if store.db.execute("SELECT 1 FROM esports_forecasts WHERE event_ticker=?", (event,)).fetchone():
            continue
        question = f"Will {team} beat {rival} in this {game} match?"
        try:
            news = searcher.context(f"{team} {rival} {game} match", team) if searcher else ""
            p, why = model.estimate(question, (m.get("rules_primary") or "")[:1500], str(m.get("expected_expiration_time") or m.get("close_time") or ""), "", news, f"{team} wins")
        except Exception as exc:  # a model that is busy or down: try again next pass
            log.warning("esports: no forecast for %s (%s)", event, exc)
            continue
        close_time = str(m.get("expected_expiration_time") or m.get("close_time") or "")
        p_rank, rank_why, ranking = None, "", ""
        if rankings is not None:
            ranking = rankings.text(game, team, rival)
            if ranking:
                try:
                    p_rank, rank_why = model.estimate(question, (m.get("rules_primary") or "")[:1500], close_time, "", f"{ranking}\n{news}", f"{team} wins")
                except Exception as exc:  # the news-only forecast stands; the ranked one is left empty for this match
                    log.warning("esports: no ranked forecast for %s (%s)", event, exc)
        bid, ask = _price(m, "yes_bid_dollars"), _price(m, "yes_ask_dollars")
        mid = (bid + ask) / 2 if bid is not None and ask is not None and ask > 0 else None
        start = start_of(m["ticker"]) or now
        store.db.execute(
            "INSERT OR IGNORE INTO esports_forecasts (event_ticker, ticker, game, team, rival, ts, start_ts, p_model, why, news, bid, ask, mid, p_rank, rank_why, ranking)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event, m["ticker"], game, team, rival, now, start, p, (why or "")[:400], news[:1800], bid, ask, mid, p_rank, (rank_why or "")[:400], ranking[:600]),
        )
        store.db.commit()
        saved += 1
        log.info("esports: %s vs %s (%s): news says %.0f%%, with rankings %s, for %s; market %s", team, rival, game, 100 * p,
                 "n/a" if p_rank is None else f"{100 * p_rank:.0f}%", team, "n/a" if mid is None else f"{100 * mid:.0f}%")
    return saved


def settle(client: Any, store: Any, now: float | None = None) -> int:
    """Read the result of every forecast whose match should be over. Returns how many were settled."""
    now = time.time() if now is None else now
    done = 0
    for r in store.db.execute("SELECT event_ticker, ticker FROM esports_forecasts WHERE outcome IS NULL AND start_ts < ?", (now - SETTLE_AFTER_H * 3600,)).fetchall():
        try:
            result = client.market(r["ticker"]).get("result")
        except KalshiError:
            continue
        if result in {"yes", "no"}:
            store.db.execute("UPDATE esports_forecasts SET outcome=?, settled_ts=? WHERE event_ticker=?", (result, now, r["event_ticker"]))
            store.db.commit()
            done += 1
    return done


def report(store: Any) -> list[str]:
    rows = store.db.execute("SELECT game, p_model, p_rank, mid, bid, ask, outcome FROM esports_forecasts").fetchall()
    settled = [r for r in rows if r["outcome"] in {"yes", "no"}]
    lines = [f"{len(rows)} matches forecast, {len(settled)} settled"]
    tight = [r for r in settled if r["mid"] is not None and r["bid"] is not None and r["ask"] is not None and (r["ask"] - r["bid"]) <= TIGHT]
    lines.append(f"{len(tight)} of those had a book within {TIGHT * 100:.0f}c, so the market's price means something")
    if len(tight) < 10:
        lines.append(f"too few to score yet (the bar is {MIN_MATCHES} matches)")
        return lines
    y = [1.0 if r["outcome"] == "yes" else 0.0 for r in tight]
    model = [(r["p_model"] - o) ** 2 for r, o in zip(tight, y, strict=True)]
    market = [(r["mid"] - o) ** 2 for r, o in zip(tight, y, strict=True)]
    coin = [(0.5 - o) ** 2 for o in y]
    diff = [a - b for a, b in zip(model, market, strict=True)]  # positive: the model did worse
    n = len(diff)
    mean = sum(diff) / n
    se = math.sqrt(sum((d - mean) ** 2 for d in diff) / (n - 1) / n)
    z = -mean / se if se else 0.0
    lines += [
        f"Brier score (lower is better): model {sum(model) / n:.4f}, market {sum(market) / n:.4f}, a coin flip {sum(coin) / n:.4f}",
        f"model minus market {mean:+.4f} ± {se:.4f} ({z:+.1f} standard errors in the model's favour)",
    ]
    both = [(r, o) for r, o in zip(tight, y, strict=True) if r["p_rank"] is not None]
    if len(both) >= 10:
        news_b = [(r["p_model"] - o) ** 2 for r, o in both]
        rank_b = [(r["p_rank"] - o) ** 2 for r, o in both]
        d = [a - b for a, b in zip(rank_b, news_b, strict=True)]  # positive: adding rankings made it worse
        dm = sum(d) / len(d)
        dse = math.sqrt(sum((x - dm) ** 2 for x in d) / (len(d) - 1) / len(d))
        lines.append(f"on the {len(both)} matches with both forecasts: news only {sum(news_b) / len(both):.4f}, news plus rankings {sum(rank_b) / len(both):.4f}, "
                     f"rankings minus news {dm:+.4f} ± {dse:.4f} ({-dm / dse if dse else 0.0:+.1f} standard errors in the rankings' favour)")
    else:
        lines.append(f"{len(both)} matches have both the news-only and the ranked forecast; too few to compare them yet")
    verdict = "the model has beaten the market by the bar" if (n >= MIN_MATCHES and z >= MIN_Z) else (
        f"not yet: the bar is {MIN_MATCHES} matches and {MIN_Z:.0f} standard errors" if n < MIN_MATCHES else "the model has not beaten the market")
    lines.append(f"verdict: {verdict}")
    return lines


class Forecaster:
    """The background thread: forecast what is about to start, settle what has finished."""

    def __init__(self, client: Any, model: Any, searcher: Any, store_factory: Any, rankings: Any = None):
        self.client, self.model, self.searcher, self.store_factory, self.rankings = client, model, searcher, store_factory, rankings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        store = self.store_factory()  # this thread's own connection
        while not self._stop.is_set():
            try:
                run_once(self.client, self.model, self.searcher, store, rankings=self.rankings)
                settle(self.client, store)
            except Exception:
                log.exception("esports: pass failed")
            self._stop.wait(EVERY)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="esports", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
