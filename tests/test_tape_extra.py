from kalshi_moonshot import config
from kalshi_moonshot.tape import TapeRecorder


class Client:
    authed = True

    def series_markets(self, series):
        return [{"ticker": f"{series}-G1", "status": "active"}, {"ticker": f"{series}-OLD", "status": "closed"}]


def test_extra_series_are_followed_on_a_second_connection_beside_the_15_minute_markets(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TAPE_EXTRA_SERIES", "KXUELGAME, KXCS2GAME")
    rec = TapeRecorder(Client(), config.load(), None)  # type: ignore[arg-type]
    rec.refresh_tickers()
    assert {"KXUELGAME-G1", "KXCS2GAME-G1"} == rec.extra_stream._tickers
    assert "KXUELGAME-OLD" not in rec.extra_stream._tickers
    assert any(t.startswith("KXBTC15M") for t in rec.stream._tickers)  # the usual markets stay on the live stream
    assert not any(t.startswith(("KXUELGAME", "KXCS2GAME")) for t in rec.stream._tickers)


def test_no_extra_series_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TAPE_EXTRA_SERIES", raising=False)
    assert config.load().tape_extra_series == ()


def test_starting_the_recorder_starts_the_second_connection_only_when_extra_series_are_named(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for extras, expected in (("KXESOCCERGAME", True), ("", False)):
        monkeypatch.setenv("TAPE_EXTRA_SERIES", extras)
        rec = TapeRecorder(Client(), config.load(), None)  # type: ignore[arg-type]
        started = []
        rec.stream.start = lambda started=started: started.append("live")  # type: ignore[method-assign]
        rec.extra_stream.start = lambda started=started: started.append("extra")  # type: ignore[method-assign]
        rec._run = lambda: None  # type: ignore[method-assign]
        rec._refresh_loop = lambda: None  # type: ignore[method-assign]
        rec.start()
        assert ("extra" in started) is expected and "live" in started


class CountingClient:
    authed = True

    def __init__(self, empty=()):
        self.calls, self.empty = [], set(empty)

    def series_markets(self, series):
        self.calls.append(series)
        return [] if series in self.empty else [{"ticker": f"{series}-G1", "status": "active"}]


def test_each_refresh_lists_only_the_series_that_are_due(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TAPE_EXTRA_SERIES", "KXESOCCERGAME,KXCS2GAME")
    client = CountingClient(empty={"KXUSDJPY15M", "KXCS2GAME"})
    rec = TapeRecorder(client, config.load(), None)  # type: ignore[arg-type]
    n_core = len(rec._series())
    rec.refresh_tickers(now=1000.0)
    assert len(client.calls) == n_core + 2  # the first look lists everything
    client.calls.clear()
    rec.refresh_tickers(now=1005.0)
    assert client.calls == []  # nothing is due yet
    rec.refresh_tickers(now=1025.0)
    assert sorted(client.calls) == sorted(s for s in rec._series() if s != "KXUSDJPY15M")  # series with markets are listed again after 20 s; the closed one and the extras are not
    client.calls.clear()
    rec.refresh_tickers(now=1100.0)
    assert "KXESOCCERGAME" in client.calls and "KXCS2GAME" not in client.calls  # extras every 90 s; an extra with nothing open only every 5 minutes
    client.calls.clear()
    rec.refresh_tickers(now=1130.0)
    assert "KXUSDJPY15M" in client.calls  # the idle 15-minute series is looked at again after 2 minutes
    assert set(rec.extra_stream._tickers) == {"KXESOCCERGAME-G1"}


def test_a_failed_listing_keeps_the_last_known_tickers(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = CountingClient()
    rec = TapeRecorder(client, config.load(), None)  # type: ignore[arg-type]
    rec.refresh_tickers(now=1000.0)
    first = set(rec.stream._tickers)
    from kalshi_moonshot.kalshi import KalshiError

    def down(series):
        raise KalshiError("down")

    client.series_markets = down  # type: ignore[method-assign]
    rec.refresh_tickers(now=1100.0)
    assert set(rec.stream._tickers) == first  # a failed request does not drop markets from the stream
