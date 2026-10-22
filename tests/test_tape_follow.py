from kalshi_moonshot import config
from kalshi_moonshot.tape import TapeRecorder


class Client:
    authed = True

    def series_markets(self, series):
        return [{"ticker": f"{series}-1", "status": "active"}]


def make(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return TapeRecorder(Client(), config.load(), None)  # type: ignore[arg-type]


def test_held_tickers_outside_the_15_minute_series_are_streamed(tmp_path, monkeypatch):
    rec = make(tmp_path, monkeypatch)
    rec.extra_tickers = lambda: {"KXGOLDH-26OCT2210-T4100"}
    rec.refresh_tickers()
    assert "KXGOLDH-26OCT2210-T4100" in rec.stream._tickers


def test_follow_adds_a_ticker_at_once_and_keeps_the_rest(tmp_path, monkeypatch):
    rec = make(tmp_path, monkeypatch)
    rec.refresh_tickers()
    before = set(rec.stream._tickers)
    rec.follow({"KXGOLDH-26OCT2210-T4100"})
    assert rec.stream._tickers == before | {"KXGOLDH-26OCT2210-T4100"}
    rec.follow({"KXGOLDH-26OCT2210-T4100"})  # already followed: nothing changes
    assert rec.stream._tickers == before | {"KXGOLDH-26OCT2210-T4100"}
