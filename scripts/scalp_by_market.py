"""Scalping by market: (1) the user's own hand trades, per series: trips, cashed-out share, win rate, net, average hold and entry price; (2) the early trend scalp replay (scalp_wave_test.py) split by coin.
Run on the host (needs the fills table and the tape)."""
import os, time, collections, statistics as S, subprocess, sys
os.nice(15)
from kalshi_moonshot import config, trips
from kalshi_moonshot.store import Store
s=config.reload(); st=Store(s.db_path,s.exclude_versions)
T=[t for t in trips.build_trips(st) if t.source=='manual' and t.closed_ts]
by=collections.defaultdict(list)
for t in T: by[t.series.replace('15M','')].append(t)
print('HAND TRADES (closed manual trips), net after fees')
print(f"{'market':10} {'trips':>5} {'win%':>5} {'cashed':>6} {'net$':>8} {'avg$':>7} {'avg entry':>9} {'avg hold min':>12}")
for k,v in sorted(by.items(),key=lambda kv:-len(kv[1])):
    n=len(v); w=sum(1 for t in v if t.pnl>0); c=sum(1 for t in v if t.cashed_out>0)
    hold=[(t.closed_ts-t.opened_ts)/60 for t in v if t.closed_ts]
    print(f"{k:10} {n:5d} {100*w/n:5.0f} {100*c/n:5.0f}% {sum(t.pnl for t in v):+8.2f} {sum(t.pnl for t in v)/n:+7.2f} {S.mean(t.entry_price for t in v):9.2f} {S.mean(hold):12.1f}")
print('DONE')
