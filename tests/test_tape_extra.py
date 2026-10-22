from kalshi_moonshot import config
from kalshi_moonshot.tape import TapeRecorder


class Client:
    authed = True

    def series_markets(self, series):
        return [{"ticker": f"{series}-G1", "status": "active"}, {"ticker": f"{series}-OLD", "status": "closed"}]


def test_extra_series_are_followed_alongside_the_15_minute_markets(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TAPE_EXTRA_SERIES", "KXUELGAME, KXCS2GAME")
    rec = TapeRecorder(Client(), config.load(), None)  # type: ignore[arg-type]
    rec.refresh_tickers()
    assert {"KXUELGAME-G1", "KXCS2GAME-G1"} <= rec.stream._tickers
    assert "KXUELGAME-OLD" not in rec.stream._tickers
    assert any(t.startswith("KXBTC15M") for t in rec.stream._tickers)  # the usual markets are still there


def test_no_extra_series_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TAPE_EXTRA_SERIES", raising=False)
    assert config.load().tape_extra_series == ()
