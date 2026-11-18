"""Commodity favourites: does sizing a bet by the market's own volatility beat a flat stake? (2026-11-18, user: "and maybe we could scale the bet by lane volatility too")

One question on the same bets as scripts/commodity_favourite_round_trips.py (4,584 commodity sides first quoted from 0.60 to 0.92, held to settlement, taker fee on the buy): the same bets staked flat, and staked in proportion to the market's own typical minute: stake = median typical minute of all bets over this bet's typical minute, so a quiet market is staked more and a wild one less. The typical minute is the rms one-second move of the mid so far, times sqrt(60), the market's own variance (rule 1). The stake multiplier is limited to 0.5 to 2 times the flat stake (a cap chosen before the run; not fitted).
Reported: net dollars and net per $100 staked for flat and for scaled stakes, the total staked, the worst day, and the result by thirds of the typical minute, since the idea only helps if the wild markets are the worse ones. Every number is held to settlement with the fee on the buy; the fills are the kindest for a taker.
Run on the Mac: .venv/bin/python scripts/commodity_stake_by_volatility.py > studies/results/<date>-commodity-stake-by-volatility.txt"""
import contextlib
import io
import runpy

import numpy as np

with contextlib.redirect_stdout(io.StringIO()):
    g = runpy.run_path("scripts/commodity_favourite_round_trips.py")
df = g["df"].copy()
net = df["held"]  # per contract, fee in
med = float(df["unit"].median())
mult = np.clip(med / df["unit"], 0.5, 2.0)
df["flat"] = net / df["ask"]  # per dollar staked
df["scaled"] = df["flat"] * mult
days = sorted(df["day"].unique())


def line(label, stake, pnl, sel):
    d = pnl[sel].groupby(df["day"][sel]).sum()
    print(f"  {label:34s} staked {stake[sel].sum():8.1f} | net {pnl[sel].sum():+8.2f} ({100.0 * pnl[sel].sum() / stake[sel].sum():+5.2f} per $100) | worst day {d.min():+7.2f} | days positive {int((d > 0).sum())} of {len(days)}")


all_ = df["unit"] > 0
print(f"window: {days[0]} to {days[-1]}; {len(df)} commodity favourites held to settlement, fee on the buy. Median typical minute {med:.2f}c; stake multiplier med/unit limited to 0.5 to 2 (mean {mult.mean():.2f}).")
line("flat $1 a bet", df["ask"] * 0 + 1.0, df["flat"], all_)
line("scaled by the typical minute", mult, df["scaled"], all_)
print("--- by thirds of the typical minute (flat stakes): are the wild markets the worse ones?")
cuts = df["unit"].quantile([1 / 3, 2 / 3]).to_list()
for name, sel in (("quietest third", df["unit"] <= cuts[0]), ("middle third", (df["unit"] > cuts[0]) & (df["unit"] <= cuts[1])), ("wildest third", df["unit"] > cuts[1])):
    line(name, df["ask"] * 0 + 1.0, df["flat"], sel)
print("DONE")
