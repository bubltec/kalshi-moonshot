#!/usr/bin/env bash
# The wave_top lane's paper ledger, one row per LOGIC ERA, so old and new scores are never mixed (2026-11-23, user: "you keep mixing up old scores and new").
# An era is the time between two pushes that changed the lane's logic; a bet belongs to the era it was BOUGHT in (a sale can land in a later one). Add a boundary below with every push that changes the lane (UTC).
# pnl_recorded = the price in the decisions table (a resting sale, reason starting 'curve exit (resting ask)', is charged no exit fee: a maker pays none); pnl_corrected = the bid in the sale's reason where that is higher (the paper sale-price bug, fixed in the 2026-11-23 15:11 UTC push, recorded rising-bid sales at a stale quote). hold_settled_only = the settled bets of the era, held to settlement.
#   AWS_PROFILE=jjosefaws scripts/wave_ledger.sh [LANE]
set -euo pipefail
cd "$(dirname "$0")/.."
lane="${1:-wave_top}"
# era boundaries, UTC, oldest first: label|start
eras=(
  "1 07:47 spike trail, k1/p1, no blackout|2026-11-23 07:47"
  "2 14:34 staircase fixes (no 5c fallback, arming)|2026-11-23 14:34"
  "3 15:11 paper sale price fix|2026-11-23 15:11"
  "4 15:40 high zone and 0.95 line removed (current)|2026-11-23 15:40"
)
case_sql="case"
for ((i=${#eras[@]}-1; i>=0; i--)); do
  label="${eras[$i]%%|*}"; start="${eras[$i]##*|}"
  case_sql+=" when ts >= extract(epoch from timestamp '$start') then '$label'"
done
case_sql+=" else '0 before 07:47 (older logic)' end"
AWS_PROFILE="${AWS_PROFILE:-jjosefaws}" scripts/cloud.sh sql "with b as (select ts, side, ask en, case when ask>0 then stake_usd/ask else 0 end c, exit_price ex, outcome o, coalesce(exit_reason like 'curve exit (resting ask)%', false) rf, nullif(substring(exit_reason from 'the bid ([0-9.]+)'),'')::float rb, $case_sql era from decisions where lane='$lane' and action='bet' and contracts is not null and stake_usd is not null and ts > extract(epoch from timestamp '2026-11-22 18:00')), p as (select *, case when ex is not null and rf then ex when ex is not null then greatest(ex, coalesce(rb, ex)) end xc, case when ex is not null or o is not null then 1 else 0 end done from b) select era, count(*) filter (where done=1) bets, count(ex) sold, round(sum(case when done=0 then 0 when ex is not null then c*(ex-en)-c*0.07*en*(1-en)-(case when rf then 0 else c*0.07*ex*(1-ex) end) else c*((o=side)::int-en)-c*0.07*en*(1-en) end)::numeric,1) pnl_recorded, round(sum(case when done=0 then 0 when ex is not null then c*(xc-en)-c*0.07*en*(1-en)-(case when rf then 0 else c*0.07*xc*(1-xc) end) else c*((o=side)::int-en)-c*0.07*en*(1-en) end)::numeric,1) pnl_corrected, round(sum(case when done=1 and o is not null then c*((o=side)::int-en)-c*0.07*en*(1-en) else 0 end)::numeric,1) hold_settled_only, count(*) filter (where done=0) not_yet from p group by 1 order by 1"
