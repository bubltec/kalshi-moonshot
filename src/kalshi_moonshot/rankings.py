"""Team rankings for the esports forecaster, from two open sources.

  CS2     Valve's Regional Standings, published as Markdown tables in github.com/ValveSoftware/counter-strike_regional_standings (live/<year>/standings_global_<date>.md)
  Dota 2  OpenDota's pro-team list (api.opendota.com/api/teams): an Elo rating, wins and losses, and when each team last played

Both are read without a key and cached for hours. `text()` turns them into a few plain lines for the model. A team that is not listed is said to be not
listed, which is itself information (Kalshi lists many lower-tier teams that neither source ranks). Any failure returns "" and the forecast goes without.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger("moonshot")

VALVE_API = "https://api.github.com/repos/ValveSoftware/counter-strike_regional_standings/contents/live/{year}"
VALVE_RAW = "https://raw.githubusercontent.com/ValveSoftware/counter-strike_regional_standings/main/live/{year}/{name}"
OPENDOTA = "https://api.opendota.com/api/teams"
TTL = 6 * 3600.0
ACTIVE_DAYS = 120  # an OpenDota team that has not played this long is not ranked among the active ones
ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|", re.MULTILINE)
FILLER = {"team", "esports", "gaming", "club", "esport"}


def norm(name: str) -> str:
    """A team name reduced to letters and digits, lower case: 'BetBoom Team' and 'BetBoom' meet, 'BIG Academy' and 'BIG' do not."""
    words = re.sub(r"[^a-z0-9 ]", "", re.sub(r"[_.]", " ", name.casefold().replace("-", ""))).split()  # a hyphen joins ("E-sports" is "esports")
    kept = [w for w in words if w not in FILLER] or words
    return "".join(kept)


def parse_valve(markdown: str) -> tuple[str, dict[str, tuple[int, int, str]]]:
    """(the date the standings are as of, {normalized name: (rank, points, name)})."""
    when = re.search(r"Standings as of (\d{4}_\d\d_\d\d)", markdown)
    return (when.group(1).replace("_", "-") if when else ""), {norm(m[3]): (int(m[1]), int(m[2]), m[3]) for m in ROW.finditer(markdown)}


def parse_opendota(teams: list[dict[str, Any]], now: float) -> dict[str, dict[str, Any]]:
    """{normalized name: {rating, wins, losses, last, rank, of}}, ranked by rating among the teams that have played lately."""
    active = sorted((t for t in teams if t.get("name") and now - float(t.get("last_match_time") or 0) <= ACTIVE_DAYS * 86400), key=lambda t: -float(t.get("rating") or 0))
    ranks = {t["team_id"]: i + 1 for i, t in enumerate(active)}
    out: dict[str, dict[str, Any]] = {}
    for t in teams:
        if t.get("name"):
            out.setdefault(norm(t["name"]), {"name": t["name"], "rating": float(t.get("rating") or 0), "wins": int(t.get("wins") or 0), "losses": int(t.get("losses") or 0),
                                             "last": float(t.get("last_match_time") or 0), "rank": ranks.get(t["team_id"]), "of": len(active)})
    return out


class Rankings:
    def __init__(self, http: httpx.Client | None = None):
        self._http = http or httpx.Client(timeout=15.0, headers={"User-Agent": "kalshi-moonshot/rankings"})
        self._lock = threading.Lock()
        self._valve: tuple[float, str, dict] | None = None
        self._dota: tuple[float, dict] | None = None

    def _valve_table(self, now: float) -> tuple[str, dict]:
        with self._lock:
            if self._valve and now - self._valve[0] < TTL:
                return self._valve[1], self._valve[2]
        year = datetime.fromtimestamp(now, UTC).year
        for y in (year, year - 1):
            listing = self._http.get(VALVE_API.format(year=y))
            if listing.status_code != 200:
                continue
            names = sorted(f["name"] for f in listing.json() if f.get("type") == "file" and f["name"].startswith("standings_global_"))
            if names:
                when, table = parse_valve(self._http.get(VALVE_RAW.format(year=y, name=names[-1])).text)
                with self._lock:
                    self._valve = (now, when, table)
                return when, table
        raise RuntimeError("no Valve standings found")

    def _dota_table(self, now: float) -> dict:
        with self._lock:
            if self._dota and now - self._dota[0] < TTL:
                return self._dota[1]
        resp = self._http.get(OPENDOTA)
        resp.raise_for_status()
        table = parse_opendota(resp.json(), now)
        with self._lock:
            self._dota = (now, table)
        return table

    def text(self, game: str, team: str, rival: str, now: float | None = None) -> str:
        """A few lines on both teams for the model, or '' when the source cannot be read."""
        now = time.time() if now is None else now
        try:
            if game == "CS2":
                when, table = self._valve_table(now)
                lines = [f"Valve Regional Standings (world ranking, as of {when}); {len(table)} teams are listed:"]
                for name in (team, rival):
                    hit = table.get(norm(name))
                    lines.append(f"- {name}: #{hit[0]} with {hit[1]} points" if hit else f"- {name}: not in the standings (outside the ranked teams)")
                return "\n".join(lines)
            if game == "Dota 2":
                table = self._dota_table(now)
                lines = ["OpenDota pro team ratings (Elo, higher is stronger):"]
                for name in (team, rival):
                    hit = table.get(norm(name))
                    if not hit:
                        lines.append(f"- {name}: no rating found")
                        continue
                    days = max(0, int((now - hit["last"]) / 86400)) if hit["last"] else None
                    rank = f"#{hit['rank']} of {hit['of']} active teams, " if hit["rank"] else "not among the active teams, "
                    lines.append(f"- {name}: rating {hit['rating']:.0f}, {rank}{hit['wins']} wins and {hit['losses']} losses on record" + (f", last played {days} days ago" if days is not None else ""))
                return "\n".join(lines)
        except Exception as exc:
            log.warning("rankings unavailable for %s (%s)", game, exc)
        return ""
