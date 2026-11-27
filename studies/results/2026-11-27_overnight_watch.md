# Overnight watch, 2026-11-27 (live PriskP lane)

User, 2026-11-27 ~01:10 ET: "let the shadow one run over night. I want you to keep an eye on wins and losses for small gains that should have been more and any losses and if we could have prevented either behavior, if it is a low risk change make it."

What "low risk" means here: a factor or bug fix inside existing logic, with a test that ends in an order where it touches a sale, the related tests passing, the replay not worse on the bets in hand, deployed only with nothing open on an unclosed market. No new step, no new gate, no fixed wave length, no cut-off on a model score. Anything bigger is written up below, not built.

Times are ET. "As run" is the sale after its fee (or settlement) less the price paid, times the contracts; the buys rested (no buy fee).

## Pass 1, 00:14 (bets bought 23:22 to 00:06)

Live build: the one rule with a profit run from two wobbles (deployed 23:46), loss sales placed, a re-buy watched as a new bet. The nested-swings shadow note and the leg's walk from before the open are committed, waiting on the full test suite, NOT deployed yet.

| Bet | Paid | Ended | Market | Note |
|---|---|---|---|---|
| DOGE 23:30 YES | 0.66 | early profit 0.69 (+2.0c) | NO | bought before the two-wobble deploy; the market settled against it, so the small sale was a save |
| ETH 23:30 YES | 0.62 | early profit 0.68 (+2.8c) | NO | the same: a save |
| SOL 23:30 YES | 0.81 | top 0.99 | YES | |
| DOGE 23:30 YES (re-buy) | 0.78 | early profit 0.80 (+0.4c) | NO | a save, by luck of the one-wobble rule |
| ETH 23:30 YES (re-buy) | 0.61 | salvage 0.12 | NO | LOSS about -$12.50. Bought back 69 s after its own profit sale, 7c under that sale, into a falling market |
| ZEC, DOGE, BNB, BTC, ETH 23:45 YES | 0.78 to 0.84 | top 0.99 | YES | five wins |
| NEAR 23:45 YES | 0.84 | early profit 0.91 (+5.0c) | YES | SMALL GAIN THAT COULD HAVE BEEN MORE: high 0.95, sold on a fall of 0.047 with 371 s left; held it paid 0.99, about +$1.40 more |
| NEAR 00:00 NO | 0.64 | no sale | not in yet | to check next pass |
| ZEC, DOGE 00:15 YES | 0.80, 0.84 | top 0.99 | not in yet | |

Noticed, nothing changed:
- The three small sales on the 23:30 markets that the user flagged as garbage were on markets that then settled against the bets. Selling small was right there by outcome; the two-wobble start would have held all three into a loss sale. One evening's bets cut both ways on that setting: leave it, score it.
- ETH 23:30 re-buy: the lane sold at 0.68 and bought the same side back at 0.61 seventy seconds later while the price was falling away. That is entry logic (a re-buy right after the lane's own exit said the move had stopped), not an exit factor. Not a low-risk change; written up for the user.
- NEAR 23:45: a 5c fall from 0.95 sold a bet that won. One bet, and the same fall rule saved the three above. No change.
