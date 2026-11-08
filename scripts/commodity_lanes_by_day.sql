-- Commodity lanes, settled bets by lane, mode and New York day (2026-11-08): net = what the bet returned (sale price x contracts if sold, else $1 x contracts if it won) minus stake_usd. Sale fees are not in it; the stake is the decision row's own.
-- scripts/cloud.sh sql "$(cat scripts/commodity_lanes_by_day.sql | grep -v '^--')"
with b as (select lane, mode, (to_timestamp(ts) at time zone 'America/New_York')::date d, stake_usd st,
  case when outcome is null or outcome='' then null
       when exit_price is not null then contracts*exit_price
       when outcome=side then contracts else 0 end pay
  from moonshot.decisions where action='bet' and lane like 'commodity%' and stake_usd>0 and ts > extract(epoch from now())-10*86400)
select lane, mode, d, count(*) n, round(sum(st)::numeric,2) staked, round(sum(pay-st)::numeric,2) net from b where pay is not null group by 1,2,3 order by 1,2,3;
