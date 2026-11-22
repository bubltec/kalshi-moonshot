# PriskP market notes: draft vocabulary (2026-11-22)

**Status: a draft for the user to correct. Nothing is built.** Built from the user's reviews of four bets (KXBTC15M-26NOV151145, 26NOV090215, 26NOV161245, KXHYPE15M-26NOV211300 and 26NOV092100) and a scan of the model's 6,149 buys (`scripts/priskp_notes_scan.py`, output `2026-11-22-priskp-notes-scan.txt`). Evidence is a lead only: conditions were chosen from the reviews before the scan, numeric cuts are the pooled upper or lower third of the model's buys (discovery cuts), and every figure is held to settlement with the fee on both legs.

## How a note works
One declarative line per market per moment, written every tick the market is evaluated, kept in memory (no database read on the tick). A buy step reads the market's notes; every buy carries the note that justified it, so the review page can show the bot's own reasoning. A `watch` is a condition a later reading can fire.

```
<verb> <side> [if <condition>] [because <readings>] [until <minute>]
```

## The list

| # | Note | Says | Source | Evidence on the model's buys (pre-buy conditions) |
|---|---|---|---|---|
| 1 | `wait` | too early, nothing has formed; do not buy yet | user: "the bot buys too soon"; 11-15 and 11-09 BTC reviews | before minute 4: -2.5c on 2,972 buys; from minute 4: -0.8c |
| 2 | `refuse thin_cushion` | the side's cushion is in the market's lowest third | 11-09 BTC (cushion +0.005), 11-09 HYPE (+0.20) | **-4.4c on 2,050 buys, negative in both halves**; the rest -0.2c |
| 3 | `wait early_thin` | before minute 4 and the cushion is thin | same | **-6.3c on 1,491 buys, both halves negative**; everything else -0.1c |
| 4 | `lean history` | in the first minutes the 15/30/60-minute history carries the decision | user: "it should have relied heavily on previous 15, 30, 60 mins" | before minute 4 with history fifth 5: +1.1c on 1,456 buys (mixed halves) |
| 5 | `refuse chop` | the spot crossed the strike 15 or more times in the hour | 11-09 BTC (11 crossings) | -1.9c on 2,269 buys, negative in both halves, but the rest is -1.4c: weak |
| 6 | `watch flip` | a side was refused; the other side may pass later | user: "didn't buy here, but a good chance this might be good in the other direction later" | **not measurable yet**: the run records only the first buy per market |
| 7 | `note spike_peak` | the spot has just spiked to its peak (15-minute move and price dip both in fifth 5) | HYPE 11-21 (bought at the peak) | -1.7c on 207 buys, both halves negative; not different from the rest (-1.6c) |
| 8 | `note counter_trend_spike` | a spike up while the 30 and 60-minute slopes are not rising | HYPE 11-21 | -0.7c on 753 buys, mixed halves; not worse than the rest |
| 9 | `note shift_down` | the level of the last 30 minutes sits far under the first 30 minutes | HYPE 11-09 ("the market shifted down and held") | -1.4c on 2,050 buys; no gradient across fifths. The step itself (size, age, whether it held) is not measured yet |
| 10 | `note off_low` | the hour's low was within the last 5 minutes (a bounce) | not from a review; the scan found it | **+3.9c on 996 buys, positive in both halves**; the rest -2.7c |
| 11 | `note at_high` | the hour's high was within the last 5 minutes | HYPE 11-21 | +0.5c on 435 buys, mixed halves |
| 12 | `note lost_recently` | the last 10 minutes were under 70% on the winning side | scan | -2.3c on 2,850 buys, both halves negative |
| 13 | `exit take_profit` | sell once the bid after its fee beats the entry price after its fee | 11-15 BTC (NO bid reached 0.90 and lost at settlement), user: "don't wait for settlement on early bets" | bid paths are recorded; `scripts/priskp_exit_study.py` is written and not yet run |
| 14 | `rebuy <side> late` | a re-buy in the last 2 minutes when the spot trends back | user: "had room for a re-buy of YES in the last 2 minutes" | not measurable yet (one buy a market) |
| 15 | `note slope_good` | falling over 30 and 60 minutes, bending up, held | 11-16 BTC ("the slope was right, and it held"; bought at minute 10.8) | -4.0c on 638 buys, both halves negative on this run (positive in an earlier superseded run); not a good note as defined |
| 16 | `fired watch` | a `watch` condition came true; the buy cites it | design | follows from #6 |

## What the scan says about this list
- Two notes look real: **thin cushion** (#2, #3) and the **bounce off the hour's low** (#10). Both agree in both halves of the days.
- The two notes from your HYPE reviews (#7, #8, #9) match the bets but are not worse than the rest of the buys, so they are not rules yet.
- **#6, #14 and #13** are the notes your reviews asked for most, and they need new runs: the first two need every moment's decision recorded, the third needs the exit study run.

## Next step proposed
Run the exit study (#13), and add to the run a log of every reading's decision (refused or bought, which check said what, and whether the other side would have passed), so #6 and #14 can be measured.
