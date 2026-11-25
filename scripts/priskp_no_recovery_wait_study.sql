WITH b AS (SELECT ticker, side, ts, stake_usd/contracts pay, contracts n, exit_ts, exit_price, outcome FROM decisions WHERE lane='priskp' AND action='bet' AND mode='trade' AND ts > 1795500000 AND (exit_ts IS NOT NULL OR outcome IS NOT NULL)),
b2 AS (SELECT *, pay+0.07*pay*(1-pay) cost, CASE WHEN exit_ts IS NOT NULL THEN exit_price-0.07*exit_price*(1-exit_price) WHEN outcome=side THEN 1.0 ELSE 0.0 END got, coalesce(exit_ts, ts+900) endts FROM b),
q AS (SELECT b2.ticker, b2.ts, b2.endts, b2.cost, t.ts qts, CASE b2.side WHEN 'yes' THEN t.bid ELSE 1-t.ask END sb FROM b2 JOIN tape.quotes t ON t.ticker=b2.ticker AND t.ts BETWEEN b2.ts AND b2.endts AND t.bid IS NOT NULL AND t.ask IS NOT NULL AND t.ask-t.bid<=0.08),
r AS (SELECT *, (sb-0.07*sb*(1-sb)) - cost net, max(CASE WHEN (sb-0.07*sb*(1-sb)) - cost > 0 THEN qts END) OVER (PARTITION BY ticker, ts ORDER BY qts) lastahead FROM q),
w AS (SELECT unnest(ARRAY[120,180,240,300,450]) W),
hit AS (SELECT r.ticker, r.ts, w.W, min(r.qts) hts FROM r CROSS JOIN w WHERE r.net <= 0 AND r.qts - coalesce(r.lastahead, r.ts) >= w.W GROUP BY 1,2,3),
alt AS (SELECT h.W, b2.n, b2.got, (SELECT r.sb FROM r WHERE r.ticker=h.ticker AND r.ts=h.ts AND r.qts=h.hts LIMIT 1) sb, b2.ts, b2.ticker FROM hit h JOIN b2 ON b2.ticker=h.ticker AND b2.ts=h.ts)
SELECT W, count(*) sold_by_rule, round(sum(n*(got))::numeric,2) as_run_value, round(sum(n*((sb-0.07*sb*(1-sb))-got))::numeric,2) delta_vs_as_run, sum((got>0.9)::int) would_have_won, sum((got<0.05)::int) zeros FROM alt WHERE sb IS NOT NULL GROUP BY W ORDER BY W;
