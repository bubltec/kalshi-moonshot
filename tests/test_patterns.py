import pytest

from kalshi_moonshot import patterns
from kalshi_moonshot.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def trade(store, i, series, won, yes_price=0.30, count=20, hold=300):
    """A bought-then-settled trade: wins pay $1 a contract, losses pay nothing."""
    ticker = f"{series}-26OCT20{i:04d}-{i:02d}"
    t0 = 1_700_000_000 + i * 3600
    store.db.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"f{i}", t0, ticker, "bid", float(count), yes_price, 1 - yes_price, 0.0, 1, f"m{i}", "buy", "yes"))
    store.db.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?)",
                     (ticker, t0 + hold, "yes" if won else "no", float(count), 0.0, 0.0, 0.0, 0.0))


def test_too_few_trades_says_so(store):
    assert patterns.analyze(store) is None
    assert "Not enough" in patterns.render(None)


def test_a_group_that_holds_all_the_larger_wins_is_a_lead(store):
    for i in range(12):                      # WTI: cheap, big winners
        trade(store, i, "KXWTI15M", won=True, yes_price=0.10, count=40)
    for i in range(12, 36):                  # gold: small winners and losers
        trade(store, i, "KXGOLD15M", won=i % 2 == 0, yes_price=0.50, count=4)
    store.db.commit()
    report = patterns.analyze(store)
    assert report is not None and report.big_wins >= 3
    leads = {(x.feature, x.value) for x in report.win_lines if x.lead}
    assert ("series", "KXWTI15M") in leads or ("market type", "energy") in leads
    text = patterns.render(report)
    assert "LEAD" in text and "KXWTI15M" in text


def test_binomial_tail_basics():
    assert patterns.binom_tail(0, 10, 0.3) == 1.0
    assert patterns.binom_tail(10, 10, 0.5) == pytest.approx(0.5**10)
    assert patterns.binom_tail(5, 10, 0.5) == pytest.approx(0.623046875)
