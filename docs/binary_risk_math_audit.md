Math audit of the binary risk model, 2026-11-20 (script scripts/binary_risk_math_audit.py, run on the runner). 4 real commodity markets (Brent, copper, gold, natgas), 3 moments each (600, 330, 200 s left).
Result: 342 checks, 0 mismatches between the code and an independent hand recomputation: the three cushions cz1/cz3/cz5 and their clipping, czb, trends tr13/tr35, pos/mv at 15/30/60, the wave shape (line slope, parabola curvature, line R^2, by a hand-solved 3x3), dr24/dr72 scaling, the NO flip, the fee 0.07p(1-p) and the ceiling (1-ask-fee)/ask. The typical minute: code vs a 1-second resample ratio 0.972-1.001 (the tape is one tick per second, median spacing 1.00 s, almost no gaps), so "RMS of one-second moves x sqrt(60)" is what is computed. Averages are plain tick means over [now-window, now]; the 24/72 h references are ticks at or before the mark (no look-ahead); the strike is the first tick at or after the open (known by the first reading, 60 s after the open).
NOT matching the assumption (finding 1): the settlement rule. On 333 sampled commodity windows, with our strike (the perp tick at the open, not Kalshi's own), the LAST-MINUTE AVERAGE against the strike agrees with the recorded result 307/333 (92%), the LAST TICK 323/333 (97%). If Kalshi settled on the last-minute average the average should agree at least as often. Either settlement here is nearer the last tick, or the strike approximation matters more for the average. The 1/3/5-minute cushion change (e3725bf) rested on "Kalshi settles on the last minute's average"; this check does not support it. Needs the user's word on the settlement text and a check with Kalshi's own strike.
Finding 2 (leakage, fixed in the new studies, not in the earlier ones): the hourly refit trained on every 60 s row of the last 120 h including rows whose market had not closed at the refit time (outcome and exit prices from after it). Size: about 0.1% of the training rows. Small, but the earlier results had it; FIX=1 in scripts/binary_risk_price_bias.py drops them.
Not checked here: the ridge standardisation and the bar on a synthetic set, the half split (the two halves are the earlier and later halves of the test hours, split at the midpoint of the hours); the target uses the contract fee on the buy and the sale and exits at the bid (checked by reading the code: NO bid = 1 - YES ask, held pays 1 or 0 with the buy fee only).

audited feeds: ['KP_BRENT', 'KP_COPPER', 'KP_GOLD', 'KP_NATGAS']  (6399 commodity windows in all)
  reference 24 h: tick at 86401 s before the open-24 h mark? (ts <= mark: True)

=== KXBRENT15M-26NOV131330-30 feed KP_BRENT strike(open+0.2s) 94.8100 result no ticks 7939
 -- 600 s left
  typical minute: 1-s resample 4.072e-04 | code (backed out of mv15) 4.066e-04 | ratio 0.999
  tick spacing over 15 min: n 898 median dt 1.00s mean 1.00s share dt>2s 0.00
 -- 330 s left
  typical minute: 1-s resample 4.678e-04 | code (backed out of mv15) 4.681e-04 | ratio 1.001
  tick spacing over 15 min: n 897 median dt 1.00s mean 1.00s share dt>2s 0.00
 -- 200 s left
  typical minute: 1-s resample 4.702e-04 | code (backed out of mv15) 4.702e-04 | ratio 1.000
  tick spacing over 15 min: n 890 median dt 1.00s mean 1.01s share dt>2s 0.00

=== KXCOPPER15M-26NOV131000-00 feed KP_COPPER strike(open+0.8s) 6.2560 result no ticks 8148
 -- 600 s left
  typical minute: 1-s resample 4.096e-04 | code (backed out of mv15) 4.076e-04 | ratio 0.995
  tick spacing over 15 min: n 893 median dt 1.00s mean 1.01s share dt>2s 0.00
 -- 330 s left
  typical minute: 1-s resample 4.375e-04 | code (backed out of mv15) 4.254e-04 | ratio 0.972
  tick spacing over 15 min: n 892 median dt 1.00s mean 1.01s share dt>2s 0.00
 -- 200 s left
  typical minute: 1-s resample 4.353e-04 | code (backed out of mv15) 4.351e-04 | ratio 1.000
  tick spacing over 15 min: n 899 median dt 1.00s mean 1.00s share dt>2s 0.00

=== KXGOLD15M-26NOV131000-00 feed KP_GOLD strike(open+0.8s) 3897.3000 result no ticks 8103
 -- 600 s left
  typical minute: 1-s resample 4.209e-04 | code (backed out of mv15) 4.210e-04 | ratio 1.000
  tick spacing over 15 min: n 889 median dt 1.00s mean 1.01s share dt>2s 0.01
 -- 330 s left
  typical minute: 1-s resample 3.934e-04 | code (backed out of mv15) 3.936e-04 | ratio 1.001
  tick spacing over 15 min: n 888 median dt 1.00s mean 1.01s share dt>2s 0.01
 -- 200 s left
  typical minute: 1-s resample 3.775e-04 | code (backed out of mv15) 3.772e-04 | ratio 0.999
  tick spacing over 15 min: n 888 median dt 1.00s mean 1.01s share dt>2s 0.01
  reference 24 h: tick at 86401 s before the open-24 h mark? (ts <= mark: True)

=== KXNATGAS15M-26NOV131330-30 feed KP_NATGAS strike(open+0.2s) 3.4630 result no ticks 7939
 -- 600 s left
  typical minute: 1-s resample 1.236e-03 | code (backed out of mv15) 1.237e-03 | ratio 1.001
  tick spacing over 15 min: n 898 median dt 1.00s mean 1.00s share dt>2s 0.00
 -- 330 s left
  typical minute: 1-s resample 1.012e-03 | code (backed out of mv15) 1.013e-03 | ratio 1.001
  tick spacing over 15 min: n 897 median dt 1.00s mean 1.00s share dt>2s 0.00
 -- 200 s left
  typical minute: 1-s resample 1.034e-03 | code (backed out of mv15) 1.034e-03 | ratio 1.000
  tick spacing over 15 min: n 890 median dt 1.00s mean 1.01s share dt>2s 0.00

settlement rule on 333 sampled commodity windows (strike = our open tick, not Kalshi's own): last-minute average agrees with the result 307/333, last tick 323/333
leakage check at refit time t: training rows are every 60 s of the previous 120 h; a row at time r in a market closing at c > t has an outcome and exit prices from after t. Share of the last 120 h's rows with close > t: about 0.10% (a row's market lasts up to 900 s, so rows in the final 15 minutes before t are affected)

SUMMARY: 0 mismatches
