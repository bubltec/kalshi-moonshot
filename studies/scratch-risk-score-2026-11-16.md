# Scratch: the risk score and the stop (2026-11-16)

Working notes, not live. Nothing here is deployed or set on a lane.

## What the stop does today

- Score: `q` = the chance the side pays. On every live lane the spot's weight is 0, so `q` is Kalshi's bid net of its fee and nothing else.
- Max-loss stop: sell when the loss, as a share of cost, reaches `0.5 x q`.
- Solved for the bid: a bet with all-in cost `c` is sold once the net bid is `c / (1 + 0.5 c)` or under.

| Bought at | Stop sells at a bid of about |
|---|---|
| 0.50 | 0.43 |
| 0.60 | 0.49 |
| 0.70 | 0.55 |
| 0.80 | 0.60 |
| 0.90 | 0.64 |

So the dearer the bet, the higher the price it is sold at. A favorite bought over about 0.62 trips the stop while the market still rates it better than even. The crypto lane adds a wait and the long layer, which carry the actual sale lower. The commodity lanes have no wait (`curve_risk_stop_hold` unset), so they sell at once.

## What the record says (114 live max-loss sales since 11-14 17:02)

| Lane group | Sold at | Sales | Would have won | Sale less hold |
|---|---|---|---|---|
| Commodity | 0.55 and over | 3 | 1 | +$7.91 |
| Commodity | 0.45 to 0.55 | 7 | 5 | -$19.36 |
| Commodity | 0.30 to 0.45 | 3 | 1 | +$1.32 |
| Commodity | under 0.30 | 5 | 0 | +$11.46 |
| Crypto | 0.30 to 0.45 | 41 | 15 | +$28.31 |
| Crypto | under 0.30 | 52 | 10 | +$7.81 |
| Crypto | within 2 min of the buy | 23 | 7 | -$48.84 |

And by cushion at the sale (crypto, 94 sales): about even in every band where the coin was across the target; -$30.12 on the 4 sales made with the coin still on the bet's side.

## Faults worth fixing, in order of evidence

1. Selling within 2 minutes of the buy (crypto: 23 sales, -$48.84; the user's 11-14 "no early loss sales").
2. Selling with the coin still on the bet's side of the target (crypto: 4 sales, -$30.12).
3. Selling a commodity favorite at 0.45 to 0.55 with no wait (7 sales, 5 would have won, -$19.36).

All three are small samples. None is proven.

## Draft: one score, no absolute rule

User: "nothing is absolute. all these factors need to build towards that risk score."

Each factor is read as a chance the bet pays, then combined into one score; the sale compares that one score with the line.

- Price: the net bid. This stays the main factor: Kalshi's price beat every other predictor tested today.
- Cushion: the spot against the target in typical moves for the time left (`scenario.cushion`), turned into a chance. It does not improve on the price for picking buys (tested), so its job here is narrow: it stops the score calling a bet lost while the coin has not crossed the target.
- Time held: a bet just bought has had no time to show anything; the score should not be acted on in its first stretch.
- Time left: already in the cushion (same distance, less time, bigger cushion) and in the wait.

Open questions before any of this is built:

- How to combine price and cushion so the cushion can raise the score but not replace the price. Candidate: score = price chance, but never under the line while the cushion is on the bet's side.
- Whether the commodity lanes should get the crypto lane's wait, or a wait of their own.
- How to score it forward: every hold and sale already writes to `data/risk_shadow.jsonl`; the scenario reading is being added to decision notes now.

## The 50-cent buys of 11-15 (for reference)

Seven live buys at asks of 0.48 to 0.56 between 02:58 and 05:58 ET on 11-15, before the edge screen was switched off. Each was bought because the ladder model's chance was 5 to 18 points over the price (for example NEAR NO at 0.52 with 72 seconds left, model 0.70). Five of the seven were then sold for a loss (0.08, 0.20, 0.14, 0.26; two sold at 0.83; one held). The ladder's chance comes from the spot against the strike with a width for volatility; near the line with little time left it is overconfident. Today's corrected studies say the same thing: the spot's cushion adds nothing to Kalshi's price.

Note: `needs_model_edge = true` (turned on today at 17:47) is this same screen. It now runs inside the 0.57 to 0.82 band.

## The 45 to 55 zone (user, 2026-11-16)

"which is a zone we should never buy in. but don't code that. it should never be desirable to pick a 50/50."

So no price cut-off. The score itself has to make a coin flip unattractive:

- A buy has to clear its costs. At 0.50 the fee and spread are the largest share of the stake of any price (about 3.5 cents on 50), so a model has to be right by the widest margin there to break even.
- A model's chance is least reliable where the spot is closest to the target, which is exactly where the price is near 0.50. The ladder was overconfident there on 11-15.
- Design consequence: the edge a buy must show should be measured against how uncertain the reading is at that point, not as a flat number. Near the target with a thin cushion the uncertainty is at its widest, so the bar is at its highest and a 50/50 never clears it. Away from the target it narrows.
- The same reasoning applies to selling: a stop that sells at 0.45 to 0.55 is trading a coin flip and paying costs to do it.

## First fit of a multi-factor score (18:45)

`scripts/risk_score_fit.py`, result in `studies/results/2026-11-16-risk-score-fit.txt`. Six factors (price, cushion, share of the market led, place in its range, 3-minute drift, time), fitted walk-forward over six held-out days, every reading strictly at or before its moment.

- Held-out log-loss: Kalshi's mid 0.5036, the score 0.5037. No gain.
- Where the score sat 3 to 6 points under the mid, YES won 62.4% against the mid's 59.6% and the score's 55.6%: the mid was closer, and the score was wrong in direction. Same the other way (36.9% actual, mid 38.0%, score 41.9%).
- Within 1 to 3 points the score was slightly closer than the mid (a small pull toward 50%).

Reading: these factors do not tell us a bet's worth better than its price does. A score built from them should not be trusted to say "sell, this is worth less than the bid". What the exit decides is therefore about risk and preference (locking a gain, not riding a loser to zero), not about knowing more than the market.

## How the exit got here (from the commits and saved lane files)

- 11-10: the curve exit stack goes live on all lanes: margin sale, trajectory, gap ("market disagrees"), time-to-go.
- 11-13 07:16: the risk exit is added in shadow: one reading, the worse of Kalshi's bid and the curve's spot chance, widened by the hour's jumpiness.
- 11-13 07:28: its max loss is made to scale with that reading (the stop formula disabled today).
- 11-13 09:26: the spot is given no weight (user: use Kalshi's price, not the outside feeds), so the reading becomes the bid alone.
- 11-13 09:45 to 11:02: the curve exit is switched off on the live lanes and the risk exit goes live (user: "risk exit should be live ... we don't want both").
- 11-13 12:48: the long layer's agreement is added as a hold.
- 11-14: the profit take is added (12:10); a swing hold is tried and removed; the lane file is pruned of switched-off exits' settings (16:21).
- 11-16 18:36: the max-loss stop is switched off on the six live lanes.

## The two exits on the bot's own bets (18:50)

`scripts/exit_eras_compare.py`, result in `studies/results/2026-11-16-exit-eras-compare.txt`. Each sale against holding the same bet.

| Stretch | Lanes | Bets | The exits against holding | Per dollar staked |
|---|---|---|---|---|
| Curve exit (11-10 to 11-13 09:45) | crypto | 287 | -$21.74 | -3.6% |
| Curve exit | commodity | 330 | -$48.08 | -2.5% |
| Risk exit (11-13 11:02 to 11-16 18:36) | crypto | 352 | +$31.70 | +0.6% |
| Risk exit | commodity | 116 | +$16.23 | +1.5% |

By kind: the curve exit's margin sale was +$16.03 on 30 crypto sales and -$41.77 on 68 commodity sales (79% of those would have won). The shock exit that ran beside it was -$27.84 on 58 crypto sales. The risk exit's profit take was +$45.73 on 114; its max-loss stop -$22.95 on 117 crypto sales and +$16.23 on 31 commodity sales.

Reading: the old stack was not doing better. The part of either exit that has earned its place is the profit take. Loss sales of every kind land near zero against holding.

## The score with the factors the user kept (19:24)

Dropped at the user's word: place in its own range, the long layer, what was paid, what is at stake. "Whether the side just flipped" replaced by the flip rate: flips so far in this market per minute lapsed (user: "not time left, time lapsed"), acting on how much the price is trusted.

Weights of the last fit: price +0.68, cushion +0.39, flip rate +0.39, time +0.06, target in the 15 min -0.42, in the 30 min -0.21, in the 60 min +0.02, last four markets' finish +0.19.

- Held out it equals Kalshi's mid overall (0.5028 against 0.5024).
- On disagreements of up to 6 points the score was the closer one; beyond 6 points Kalshi's mid was.
- The flip rate's sign is the opposite of the guess: with no flips the price is trusted LESS (weight 0.68), and it is trusted more as the market flips more. A leader that has never been challenged is the one that is a little overpriced.
- Along ETH 16:00 YES and DOGE 18:15 NO the score's risk stays within 1 to 7 points of Kalshi's own.
