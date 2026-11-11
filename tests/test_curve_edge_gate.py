import math

from kalshi_moonshot import variants


def series(now, value_at, n=1000):
    ts = [now - n + k for k in range(n + 1)]
    return ts, [value_at(t) for t in ts]


def test_a_coin_reads_its_index_against_the_strike(monkeypatch):
    now = 1_000_000.0
    ts, vs = series(now, lambda t: 100.0 * math.exp(0.00005 * math.sin(t / 7.0)) * 1.002)  # about 20 bp over a strike of 100, small noise
    monkeypatch.setattr(variants.ticks_mod.history, "window", lambda index, a, b: (ts, vs))
    monkeypatch.setattr(variants, "crypto_index", lambda s: "BRTI" if s == "KXBTC15M" else None)
    p_yes, edge_yes = variants.curve_edge_now("KXBTC15M", "yes", 300.0, 0.70, 100.0, now)
    p_no, edge_no = variants.curve_edge_now("KXBTC15M", "no", 300.0, 0.70, 100.0, now)
    assert p_yes > 0.9 and abs(edge_yes - (p_yes - 0.70)) < 1e-12
    assert p_no < 0.1 and edge_no < -0.5 and abs(p_yes + p_no - 1.0) < 1e-9


def test_a_commodity_reads_its_price_against_the_window_open(monkeypatch):
    now = 2_000_000.0
    left = 300.0
    opened = now + left - 900.0
    ts, vs = series(now, lambda t: 50.0 * (1.0 + 0.00002 * math.sin(t / 5.0)) * (1.0 if t < opened + 200 else 0.998))  # fell 20 bp after the open
    monkeypatch.setattr(variants.ticks_mod.history, "window", lambda index, a, b: (ts, vs))
    monkeypatch.setattr(variants, "crypto_index", lambda s: None)
    monkeypatch.setattr(variants, "biquote_index", lambda s: "BQ_SILVER")
    p_no, edge_no = variants.curve_edge_now("KXSILVER15M", "no", left, 0.60, None, now)
    assert p_no > 0.5 and edge_no == p_no - 0.60


def test_nothing_to_read_is_none(monkeypatch):
    monkeypatch.setattr(variants, "crypto_index", lambda s: "BRTI")
    monkeypatch.setattr(variants.ticks_mod.history, "window", lambda index, a, b: None)
    assert variants.curve_edge_now("KXBTC15M", "yes", 300.0, 0.7, 100.0, 1.0) is None
    monkeypatch.setattr(variants.ticks_mod.history, "window", lambda index, a, b: ([1.0], [100.0]))
    assert variants.curve_edge_now("KXBTC15M", "yes", 300.0, 0.7, None, 1_000_000.0) is None  # no strike
    assert variants.curve_edge_now("KXBTC15M", "yes", 0.0, 0.7, 100.0, 1_000_000.0) is None
    monkeypatch.setattr(variants, "crypto_index", lambda s: None)
    monkeypatch.setattr(variants, "biquote_index", lambda s: None)
    assert variants.curve_edge_now("KX2YRRATE15M", "yes", 300.0, 0.7, 4.9, 1_000_000.0) is None  # no index
