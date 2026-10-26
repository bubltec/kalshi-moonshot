from kalshi_moonshot import rankings

VALVE = """### Standings as of 2026_10_05<br />

| Standing | Points | Team Name            | Roster |   |
| :- | -: | :- | :- | :- |
| 1        |   2046 | Spirit               | donk, magixx | [details](x) |
| 8        |   1780 | G2                   | HeavyGod     | [details](y) |
"""
NOW = 1_792_900_000.0
TEAMS = [
    {"team_id": 1, "rating": 1623.1, "wins": 223, "losses": 139, "last_match_time": NOW - 3 * 86400, "name": "Team Yandex"},
    {"team_id": 2, "rating": 1500.0, "wins": 10, "losses": 4, "last_match_time": NOW - 400 * 86400, "name": "Old Squad"},
    {"team_id": 3, "rating": 1400.0, "wins": 50, "losses": 40, "last_match_time": NOW - 10 * 86400, "name": "BetBoom Team"},
]


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status
        self.text = body if isinstance(body, str) else ""

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(self.status_code)


class Http:
    def __init__(self, fail=False):
        self.fail, self.urls = fail, []

    def get(self, url):
        self.urls.append(url)
        if self.fail:
            raise RuntimeError("down")
        if "api.github.com" in url:
            return Resp([{"name": "standings_global_2026_09_07.md", "type": "file"}, {"name": "standings_global_2026_10_05.md", "type": "file"}, {"name": "details", "type": "dir"}])
        if "raw.githubusercontent" in url:
            assert url.endswith("standings_global_2026_10_05.md")  # the latest file
            return Resp(VALVE)
        return Resp(TEAMS)


def test_names_meet_when_only_filler_differs():
    assert rankings.norm("BetBoom Team") == rankings.norm("BetBoom") == "betboom"
    assert rankings.norm("BIG Academy") != rankings.norm("BIG")
    assert rankings.norm("ARK E-sports") == "ark"


def test_the_valve_table_is_read():
    when, table = rankings.parse_valve(VALVE)
    assert when == "2026-10-05" and table["spirit"][:2] == (1, 2046) and table["g2"][:2] == (8, 1780)


def test_opendota_ranks_only_teams_that_have_played_lately():
    table = rankings.parse_opendota(TEAMS, NOW)
    assert table[rankings.norm("Team Yandex")]["rank"] == 1
    assert table[rankings.norm("Old Squad")]["rank"] is None and table[rankings.norm("BetBoom Team")]["rank"] == 2 and table["betboom"]["of"] == 2


def test_the_text_tells_the_model_what_is_known_and_what_is_not():
    r = rankings.Rankings(Http())
    cs = r.text("CS2", "G2", "overTIME", NOW)
    assert "as of 2026-10-05" in cs and "G2: #8 with 1780 points" in cs and "overTIME: not in the standings" in cs
    dota = r.text("Dota 2", "BetBoom Team", "Nobody FC", NOW)
    assert "rating 1400, #2 of 2 active teams, 50 wins and 40 losses" in dota and "last played 10 days ago" in dota and "Nobody FC: no rating found" in dota


def test_a_table_is_fetched_once_then_cached():
    h = Http()
    r = rankings.Rankings(h)
    r.text("CS2", "G2", "X", NOW)
    r.text("CS2", "Spirit", "Y", NOW + 60)
    assert sum("api.github.com" in u for u in h.urls) == 1


def test_a_failure_gives_no_text_instead_of_an_error():
    assert rankings.Rankings(Http(fail=True)).text("CS2", "G2", "X", NOW) == ""
    assert rankings.Rankings(Http()).text("Valorant", "A", "B", NOW) == ""

