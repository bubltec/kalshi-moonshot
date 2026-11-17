-- Live bets since 2026-11-09 by how each one ended, against holding the same bets to settlement (user, 2026-11-17: "Yes, break it down").
-- Run with: scripts/cloud.sh sql "$(cat scripts/live_bets_by_ending.sql) select grp, ending, count(*), sum(traded), sum(held) from e group by 1,2 order by 1,2"
-- e: one row per live bet (mode trade, a stake, a settled market). traded: the result as it happened (scorecard.bet_pnl's formula, the fee on each leg). held: the same bet held to settlement. won: the side's market result.
with b as (
  select d.*, w.result, (w.result = d.side) won, d.stake_usd/d.ask c,
         case when d.lane like 'crypto%' then 'crypto' when d.lane like 'commodity%' then 'commodity' else 'other' end grp
  from decisions d join windows w on w.ticker = d.ticker
  where d.action='bet' and d.mode='trade' and d.stake_usd>0 and d.ask>0 and w.result in ('yes','no')
    and d.ts >= extract(epoch from timestamp with time zone '2026-11-09 00:00:00-05')
), p as (
  select b.*,
    case when won then c*(1-ask) - c*0.07*ask*(1-ask) else -stake_usd end held,
    case when exit_ts is not null then c*(exit_price-ask) - c*0.07*greatest(exit_price,0.01)*(1-greatest(exit_price,0.01)) - c*0.07*ask*(1-ask)
         when won then c*(1-ask) - c*0.07*ask*(1-ask) else -stake_usd end traded
  from b
), e as (
  select p.*, case when exit_ts is null and won then '1 held, won' when exit_ts is null then '2 held, lost' when traded > 0 then '3 sold ahead' else '4 sold behind' end ending from p
)
