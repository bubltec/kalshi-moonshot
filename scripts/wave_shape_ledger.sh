#!/usr/bin/env bash
# wave_shape paper ledger (2026-11-24): own-exit P&L by branch (R repeated swing / S one wide swing) and by the turn reading at the buy
# (given back = the share of the swing already given back from the broad extreme; 0.00 = bought while the extreme was still being made).
# Fees: no entry fee (the buys are resting, maker orders; paperfill still sizes contracts as if it paid one, so this is the live-like figure); 0.07*p*(1-p) on a taker sale; a resting sale (reason 'curve exit (resting ask)') pays no exit fee. Open bets are left out. Not held to settlement:
# the lane does not hold.
set -euo pipefail
cd "$(dirname "$0")/.."
AWS_PROFILE="${AWS_PROFILE:-jjosefaws}" scripts/cloud.sh sql "
with b as (
  select ts, ticker, side, stake_usd/nullif(contracts,0) en, contracts c, exit_price ex,
         coalesce(exit_reason like 'curve exit (resting ask)%', false) rf,
         substr(reason, position('wave_shape yes (' in reason)+16, 1500) rd
  from moonshot.decisions where lane='wave_shape' and action='bet' and stake_usd is not null and exit_price is not null),
p as (
  select *, case when rd like 'wave shape R%' then 'R' else 'S' end br,
         coalesce(substring(rd from 'turn [0-9]+s ago with ([0-9.]+) given')::float, -1) gb,
         c*(ex-en) - case when rf then 0 else c*0.07*ex*(1-ex) end pnl from b)
select br, case when gb<0 then 'n/a' when gb<0.02 then 'given back <0.02' else 'given back >=0.02' end turn, count(*) bets,
       round(sum(pnl)::numeric,2) pnl, count(*) filter (where pnl>0) wins
from p group by 1,2 order by 1,2"

# Same-bets view (rule 4): wave_live is a paper lane that bets on the same markets with the old wave_flag buy step. For each closed wave_live bet since
# wave_shape started (2026-11-24 11:45 ET), did wave_shape buy the same market and side within 120 s? Own-exit P&L of the wave_live bet (no entry fee: resting
# buys), split by that. 'rule skipped' = what wave_shape left out; 'rule bought' = what it also took.
AWS_PROFILE="${AWS_PROFILE:-jjosefaws}" scripts/cloud.sh sql "
with l as (
  select ts, ticker, side, stake_usd/nullif(contracts,0) en, contracts c, exit_price ex,
         coalesce(exit_reason like 'curve exit (resting ask)%', false) rf
  from moonshot.decisions where lane='wave_live' and action='bet' and stake_usd is not null and exit_price is not null
    and ts > extract(epoch from timestamp '2026-11-24 16:45')),
s as (select ticker, side, ts from moonshot.decisions where lane='wave_shape' and action='bet' and stake_usd is not null),
p as (
  select l.*, exists(select 1 from s where s.ticker=l.ticker and s.side=l.side and abs(s.ts-l.ts)<=120) bought,
         c*(ex-en) - case when rf then 0 else c*0.07*ex*(1-ex) end pnl from l)
select case when bought then 'rule bought' else 'rule skipped' end as wave_live_bets, count(*) bets, round(sum(pnl)::numeric,2) pnl,
       count(*) filter (where pnl>0) wins, round((sum(pnl)/nullif(sum(c*en),0))::numeric,3) per_dollar
from p group by 1 order by 1"
