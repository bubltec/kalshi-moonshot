"""One held bet and the market around it, small enough to keep in the repository."""

from __future__ import annotations

import base64
import gzip
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BQ = {"GOLD": "BQ_GOLD", "SILVER": "BQ_SILVER", "PLATINUM": "BQ_PLATINUM", "PALLADIUM": "BQ_PALLADIUM", "WTI": "BQ_WTI", "BRENT": "BQ_BRENT", "NATGAS": "BQ_NATGAS", "COPPER": "BQ_COPPER", "INX": "BQ_SPX"}
QUOTES_BEFORE_CLOSE = 240.0  # seconds of quotes kept ahead of the close (and from the buy, if later)
TICKS_BEFORE_CLOSE = 300.0  # seconds of index ticks kept: the shock lookback and its memory, and the variety's run-up are inside this


@dataclass
class Tape:
    name: str
    ticker: str
    lane: str
    side: str  # "yes" | "no"
    entry: float  # the ask the decision saw
    stake_usd: float
    close_ts: float
    index_id: str  # the spot index the exits read
    ref: float  # what the cushion is measured from: the strike (crypto) or the index price at the window open (commodities)
    result: str  # "yes" | "no" | "" while unsettled
    quotes: list[list[float]] = field(default_factory=list)  # [ts, yes_bid, yes_ask]
    ticks: list[list[float]] = field(default_factory=list)  # [ts, value]
    flow: list[list[float]] = field(default_factory=list)  # [ts, yes_taker_contracts, no_taker_contracts, vwap_yes_price] per second: what the burst exit reads
    bought_ts: float = 0.0
    variety: str = ""  # variants.py letter at the buy, "" when unknown
    note: str = ""  # what happened live, in a line

    @property
    def contracts(self) -> float:
        return self.stake_usd / self.entry if self.entry > 0 else 0.0

    def hold_pnl(self) -> float:
        """What holding to the close paid, before fees on the buy: contracts x (1 - entry) on a win, the stake lost otherwise."""
        if self.result not in ("yes", "no"):
            return 0.0
        return self.contracts * (1.0 - self.entry) if self.result == self.side else -self.stake_usd

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), separators=(",", ":")) + "\n")

    @classmethod
    def load(cls, path: Path) -> Tape:
        return cls(**json.loads(path.read_text()))

    def pack(self) -> str:
        """gzip+base64 on one line, for carrying a capture out of a host shell whose output is capped."""
        return base64.b64encode(gzip.compress(json.dumps(asdict(self), separators=(",", ":")).encode())).decode()

    @classmethod
    def unpack(cls, blob: str) -> Tape:
        return cls(**json.loads(gzip.decompress(base64.b64decode(blob)).decode()))


def capture(store: Any, tape_db: Any, ticker: str, lane: str, name: str = "", note: str = "") -> Tape:
    """Build a Tape from the live databases: the bet's decision row, its window, and the quotes and index ticks around the close."""
    from ..kalshiws import index_for_series

    d = store.db.execute("SELECT ts, side, ask, stake_usd, close_time FROM decisions WHERE action='bet' AND ticker=? AND lane=? ORDER BY ts DESC LIMIT 1", (ticker, lane)).fetchone()
    if d is None:
        raise SystemExit(f"no bet on {ticker} in lane {lane}")
    w = store.db.execute("SELECT close_ts, strike, series, result FROM windows WHERE ticker=?", (ticker,)).fetchone()
    close = float(w["close_ts"]) if w else datetime.fromisoformat(d["close_time"]).timestamp()  # a window row appears only once the market is settled
    series = ticker.split("-")[0]
    commodity = series[2:-3] in BQ
    index_id = BQ[series[2:-3]] if commodity else (index_for_series(series) or "")
    if not index_id:
        raise SystemExit(f"no spot index for {series}")
    if commodity:  # the same read the watcher makes at arming: the first tick within a few seconds of the window open
        r = tape_db.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts LIMIT 1", (index_id, close - 905.0, close - 880.0)).fetchone()
        ref = float(r[0]) if r else 0.0
    else:
        ref = float(w["strike"] or 0.0) if w else 0.0
    bought = float(d["ts"])
    q = tape_db.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, max(bought, close - QUOTES_BEFORE_CLOSE), close)).fetchall()
    k = tape_db.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (index_id, close - TICKS_BEFORE_CLOSE, close)).fetchall()
    fl = tape_db.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (ticker, close - TICKS_BEFORE_CLOSE, close)).fetchall()
    return Tape(
        flow=[[float(x[0]), float(x[1]), float(x[2]), float(x[3] or 0.0)] for x in fl],
        name=name or f"{ticker}-{lane}", ticker=ticker, lane=lane, side=d["side"] or "yes", entry=float(d["ask"]), stake_usd=float(d["stake_usd"]),
        close_ts=close, index_id=index_id, ref=ref, result=(w["result"] or "") if w else "", bought_ts=bought,
        quotes=[[round(float(x[0]), 3), float(x[1]), float(x[2])] for x in q], ticks=[[round(float(x[0]), 3), float(x[1])] for x in k], note=note,
    )


def close_time_iso(close_ts: float) -> str:
    return datetime.fromtimestamp(close_ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
