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

## NEAR 00:00 NO, a zero the user flagged at ~00:20 ("this cannot happen. Why are we still getting 0s???")

Bought NO at 0.64 at 23:51:21 (519 s left). The bid stood at 0.83 to 0.88 from 23:53:10 to 23:56:20 (about +22c after both fees, for three minutes), fell to 0.67 by 23:57:20, was 0.34 ten seconds later, bounced to 0.60 at 23:58:20 and went to 0. No sale. About -$15.

What the rule read (traced on the bet's own quotes and index, with the target taken from the market's page, 3.1044, because the windows row was not in yet; the cushion figures depend on that):
- 23:53:33 to 23:57:27: HELD BY THE CUSHION. The spot's 60 s average was a sigma of the time left on the bet's side, and the rule holds a profit there whatever the bid does (user, 2026-11-25: "with that much cushion it should not sell, for almost anything"). The bid fell 0.23, almost six wobbles, while the index still read as safe: the bid moved first.
- The fade would have held it as well: the bet was two thirds of the way through its life, where early profit is a tenth (the user's own rule, to stop late profits being sold).
- Under cost from 23:57:27: NO LOSS, the spot had not crossed the target by a wobble on both its 60 s and 5 s averages. It crossed in the last seconds.

Two fixes tried on the tape, neither shipped:
1. A profit floor that never fades and that no cushion holds (sell when the profit is down to its last wobble): the 210 bets fall from +$149.66 to +$59.73.
2. The same floor only when the fall is also large for the market: +$88.39. It makes 27 sales, and 23 of those 27 markets went on to settle the bet's way: a bet that falls back to its cost after a good profit usually still wins.
So protecting against this zero costs about $60 to $90 on a day and a half of bets. Not a low-risk change; nothing was changed. Decision for the user.

Worth looking at next (not built): on this bet the bid collapsed while the index still showed a cushion. The cushion hold trusts the index's 60 s average absolutely once it is a sigma out. Whether a large fall of the bid should weaken that hold is a question about which of the two leads, to be answered on the tape, not by a number of mine.

## Pass 2, 00:24 (bets bought 00:17 to 00:21, the 00:30 markets)

Live build: still the 23:46 one. The stall change (35f4b558), the nested-swings shadow note and the leg's walk from before the open are on main and pushed, NOT deployed: the user questioned the stall change at ~00:23 ("That's not what I said exactly... What did you do?"), so the 00:30 deploy was stopped and waits for their answer.

| Bet | Paid | Ended | As run |
|---|---|---|---|
| ZEC 00:30 NO, 00:17:13 | 0.73 | salvage 0.33 at 00:21:20 | -$9.18 |
| BTC 00:30 NO, 00:17:15 | 0.64 | salvage 0.27 | -$9.08 |
| BNB 00:30 NO, 00:17:34 | 0.58 | salvage 0.18 | -$11.11 |
| ETH 00:30 NO, 00:17:52 | 0.65 | salvage 0.28 | -$9.52 |
| XRP 00:30 NO, 00:17:53 | 0.63 | salvage 0.17 | -$10.79 |
| NEAR 00:30 NO, 00:18:02 | 0.62 | salvage 0.15 | -$13.31 |
| SOL 00:30 YES, 00:18:42 | 0.71 | early profit (stall) 0.84 | +$0.75 (7.1 contracts) |
| BTC 00:30 YES, 00:21:22 | 0.72 | early profit (fall) 0.81 | +$1.14 |
| DOGE 00:30 YES, 00:21:28 | 0.67 | open at the pass | |

SIX NO BETS IN 50 SECONDS, ALL LOST: about -$63. The loss sales worked (salvage sold each at 0.15 to 0.33 inside five minutes, about $35 back of $94 staked; before 22:47 these would have gone to zero), but the entries are the problem, as the user said of the 22:30 round ("It's an entry issue when they all fail like that").

What the curve was doing: in the 20 minutes before these buys the spot had RISEN 2.5 to 8.1 of its typical minutes against every one of the six (BTC -7.3, BNB -8.1, ETH -7.8, XRP -6.5, ZEC -3.4, NEAR -2.5 for the NO side). Two minutes into the market the lane read a small dip as a down swing from a turn and bought NO on all six within 50 seconds. The same shape as BTC, ZEC and DOGE 22:30 and BTC and BNB 23:00.

What the readings would have said (computed after, from the index tape; the shadow note is not deployed):
- nested swings: BNB, ETH, XRP, NEAR read "- + - +", 2 of 4 with the bet: shadow would WAIT. ZEC and BTC read "- + + +", 3 of 4: would have bought too. Four of six.
- the leg walked from before the open (the change waiting to deploy): would not have waited on these; the small down swing began after the open, so it is young however far back the walk starts.
- side_losing let all six through: each was seconds old and none was 5% under yet when the next was bought.

Not a low-risk change, nothing changed. For the user: six same-side buys inside one minute are one bet at six times the stake, and nothing in the buy module sees the run the market had just made the other way.

## Pass 3, 00:49 (bets bought 00:21 to 00:47)

Deployed since pass 2: 00:33 the nested-swings shadow note and the leg's walk from an hour before the open; 00:45 the "profit gained too fast" clause (FAST_PACE 2, the user's rule); 00:48 the same again with the shadow note's "at a peak / off the peak" wording (the user: "Just deploy again now"). The stall change was set aside on the branch stall-before-cushion, never deployed. ZEC 01:00 NO (0.73, bought 00:47:53) was open across the 00:48 restart.

About -$26 as run on ten closed bets.

| Bet | Paid | Ended | Market | As run | Note |
|---|---|---|---|---|---|
| BTC 00:30 YES 00:21:22 | 0.72 | early profit 0.81 | YES | +$1.14 | small gain, the market won: about $4 more held |
| DOGE 00:30 YES 00:21:28 | 0.67 | early profit 0.72 | YES | +$0.86 | the same, about $6 more held; the lane bought it back at 0.82 |
| DOGE 00:30 YES 00:26:49 | 0.82 | top 0.99 | YES | +$3.25 | the buy-back |
| HYPE 00:30 YES 00:27:24 | 0.68 | no sale | NO | about -$15 | A ZERO. Bought at minute 12.4 at the top of a 50 s spike (0.45 to 0.69), chance 0.519; the bid then swung 0.31 to 0.75 and went to 0 inside the last minute |
| ZEC 00:45 YES | 0.70 | early profit 0.72 | YES | +$0.47 | small gain, the market won |
| HYPE 00:45 YES | 0.58 | early profit 0.65 | NO | +$1.19 | a save |
| SOL 00:45 YES | 0.78 | early profit 0.84 | NO | +$0.56 | a save |
| BNB 00:45 YES 00:35:38 | 0.84 | salvage 0.32 | YES | -$9.86 | bought at the top (0.85), fell to 0.26, salvaged at 0.32, then the market ran to 1.00: held it paid +$2.9 |
| ETH 00:45 YES 00:36:06 | 0.65 | salvage 0.09 | NO | -$9.84 | the user's "another bad buy": the top of the fourth bounce of a fade |
| BTC 00:45 YES 00:42:32 | 0.63 | early profit (stall) 0.71 | YES | +$1.64 | |

Noticed:
- All three losses were BOUGHT AT A PEAK of their own swing: HYPE 00:30 at the top of a spike with under three minutes left, BNB 00:45 at 0.84 with the bid at its high, ETH 00:45 at the top of a bounce. This is the user's point ("It also bought on a peak which it should never do for a favorite") and the table's (off-the-peak buys made about four times as much a bet). The shadow note on BNB and ETH said "3 of 4 with: would buy too" (that build had no peak wording yet). The wait for a pullback is put to the user; it is entry logic, not a low-risk change.
- HYPE 00:30 went to zero with no sale: bought with 156 s left, it was never two wobbles ahead and the spot did not cross by a wobble on both averages until the settlement window. A buy that late on a 0.52 chance has no room for any exit to work; again an entry matter.
- BNB 00:45: the salvage sold at 0.32 a bet that then won. Known weak spot (the replay's salvage sales came out behind what those bets made). One bet; no change.
- Small gains that could have been more: BTC and DOGE 00:30 and ZEC 00:45 sold 2 to 5.5c ahead on markets that then won; HYPE and SOL 00:45 sold the same way on markets that lost. Three against two this pass; no change.

Nothing changed this pass.

## Pass 4, 01:25 (bets bought 00:47 to 01:23)

Deployed since pass 3: 01:00 the curve entry's wait at the peak of the bet's own swing (the user: "I think we should set the off the peak after a pullback on") and the order journal's buy order row on a recovered bet (SOL 00:45 YES had gone to zero because the 00:33 push orphaned its resting buy and its sale at 0.84 was booked on paper); 01:24 the loss side's cross measured against the best cushion of the whole market (the user, on BNB 00:45) and the fast clause selling only once the bid stops climbing (the user, on SOL 01:15), with another session's paper wave_dip step that had landed on main.

About +$23 as run on seven closed bets, no loss.

| Bet | Paid | Ended | As run | Nest note at the buy |
|---|---|---|---|---|
| ZEC 01:00 NO 00:47:53 | 0.73 | early profit 0.79 | +$0.72 | 3 of 4 |
| ZEC 01:00 NO 00:52:48 | 0.84 | top 0.99 | +$2.67 | 4 of 4, at a peak (a steady-market buy: the peak wait is the curve's only) |
| BTC 01:15 YES 01:02:16 | 0.64 | top 0.99 with 164 s left | +$7.90 | 2 of 4, off the peak |
| SOL 01:15 YES 01:11:36 | 0.73 | fast 0.91, 36 s after the buy | +$3.30 | 2 of 4, off the peak. The bid was still rising and went to 1.00 (about +$5.10 held): the user's "It shouldn't sell if it doesn't stop", fixed at 01:24 |
| SOL 01:30 NO 01:17:33 | 0.66 | fast 0.86 | +$4.20 | 2 of 4, off the peak (sold before the 01:24 change) |
| XRP 01:30 NO 01:17:38 | 0.59 | early profit 0.69 | +$1.65 | 2 of 4, off the peak |
| DOGE 01:30 NO 01:21:18 | 0.66 | fast 0.81 | +$2.80 | 2 of 4, off the peak |
| DOGE 01:30 NO 01:23:29 | 0.84 | open at the pass | | 2 of 4, off the peak |

Noticed:
- Since the peak wait went live at 01:00 every curve buy's note reads "off the peak", and there are fewer buys: six in the 25 minutes from 01:00 against ten to twelve in the 25-minute stretches before. None of the six lost. Far too few to call; it is the first sign the wait changes when the lane buys, as the user expected.
- The shadow's "2 of 4 with the bet: would WAIT (a bounce against the larger move)" was on five of these six winners. On tonight's bets that shadow line is wrong more often than right; the peak reading is the part that is holding up. To be judged on more bets before anything is done with it.
- BTC 01:15 sold at 0.99 with 164 s left (the user: "didn't we get it to stop this?"). The locked-in hold is the last minute's only. On the 210 bets, widening it to hold at 0.99 whenever the spot's cushion for the time left makes holding worth more than the bid changes 11 bets, all for the better (+$1.61), where removing the top clause gains $9.17 on 58 bets and loses $45.60 on 3. The wider hold is a factor of the existing test and is being built after the test run in progress.

Nothing changed in this pass itself.

## Pass 5, 01:49 (bets bought 01:21 to 01:48)

Deployed since pass 4: 01:45 the top's hold at any minute when the cushion makes holding worth more than the bid (the user, on BTC 01:15 sold at 0.99 with 164 s left). That deploy also carried the shared .env with LIVE_LANES=priskp,wave_shape: another session put wave_shape live at $2 a bet (said to the user before the deploy; no answer came).

PriskP: two closed bets, both gains, about +$5; NO PRISKP BUY SINCE 01:23:29.

| Bet | Paid | Ended | As run |
|---|---|---|---|
| DOGE 01:30 NO 01:21:18 | 0.66 | fast 0.81 | +$2.80 |
| DOGE 01:30 NO 01:23:29 | 0.84 | fast 0.97 (after the 01:24 change: sold once the bid stopped climbing) | +$2.20 |

Noticed:
- Twenty-five minutes with no PriskP buy (the whole 01:45 market and the first four minutes of the 02:00 one). The looks are running: since 01:24:30 the lane's passes read curve_wait 6899, dead_ask_band 458, check_not_passing 450, window 430, curve_not_held 81, cushion_not_strong 48, curve_peak 36, confirming 31. So the new peak wait is a small part of it (36 looks); the curve's own wait is what is holding, at about the rate it did before (7499 in the 25 minutes from 00:35, when the lane was buying). It may be the markets of this half hour; it may be the leg reading's longer walk (live since 00:33) calling more legs old. Not enough to tell, and nothing to change on one quiet stretch: to be watched on the next passes. If the lane stays silent through 02:15 the user is told.
- The DOGE 01:23:29 sale is the first by the fast clause as changed: it held from the pace being reached until the bid stopped, and sold at 0.97.
- wave_shape (not this watch's lane; the other session's, live from about 01:36 at $2): eight closed bets, about -$2 in all (five give-ups at a loss, a turn sale, a value sale and a salvage at a gain).

Nothing changed this pass.

## Pass 6, 03:19 (bets bought 01:49 to 03:17; the passes between did not run)

Live build unchanged since 01:45 (51c09740). The full test suite on it passed: 1746 passed, 5 skipped.

PriskP: 21 closed bets, 20 gains and one total loss, about +$48 as run. The quiet stretch of pass 5 ended at 01:49:53 without anything being changed.

- Eight sold at the top (0.98 to 0.99): about +$34.
- Ten sold by the fast clause once the bid stopped climbing, at 0.78 to 0.98: about +$23. Two of them (SOL 02:45 YES at 0.94, HYPE 02:30 NO at 0.78... the latter won) were on markets that then went against the bet: SOL 02:45 settled NO, so its sale was a save of a whole stake.
- Two early profits: ETH 02:15 NO sold at 0.83 on a stall (+$5.80; the market then settled YES, a save), BNB 02:30 NO at 0.76 (+$0.70; the market won).
- ONE LOSS, a zero: DOGE 02:45 YES, bought 0.78 at 02:34:56 (a steady-market buy, chance 0.66, the note "4 of 4 with the bet ... at a peak"). The bid reached 0.87 (+7c, under two wobbles, so no profit run), went 0.87 to 0.25 in ninety seconds, bounced to 0.32 and failed, and went to 0. About -$15.20. No sale: under cost the loss side now asks for the spot to be as far under the target as it had been over it in the market (live since 01:24), and this market's cushion had been large. Under the cross test as it was before, the failed bounce near 0.27 would have been salvaged, about $3.70 better on this one bet. One bet against the 13-for-37 fewer salvage sales of the replay; not changed, to be counted with the next losses.

Noticed:
- Every curve buy of this pass was "off the peak" (the wait), and the four buys noted "at a peak" were steady-market buys, where the wait does not act: of those four, three were gains and one is the zero above. The table said a peak made no difference outside a curve (91% against 93%); four bets do not change that, but the zero is the kind of buy the user called bad. Left for the user.
- No sale at 0.99 was held by the wider top hold in the reasons seen here; all eight tops sold.
- wave_shape (the other session's lane, live at $2 a bet since about 01:36): 44 closed bets, about -$15.90. Not this watch's lane; told to the user.

Nothing changed this pass.

## Pass 7, 03:49 (bets bought 03:17 to 03:48)

Live build unchanged (51c09740).

PriskP: one closed bet, a gain of about +$1.50 (SOL 03:30 YES, 0.84, sold by the fast clause at 0.93 once the bid stopped; the market settled YES, about $1.10 more held). One open at the pass (SOL 04:00 YES, 0.68, a curve buy off the peak). No buy between 03:17:12 and 03:48:39, thirty-one minutes; the earlier quiet stretch (01:23 to 01:49) ended by itself, so nothing is read into this one.

wave_shape (the other session's lane): 57 closed bets, about -$17.50.

Nothing to trace (no loss, no small gain that ran far), nothing changed.

## Pass 8, 04:19 (bets bought 03:48 to 04:18)

Live build unchanged at the pass (51c09740).

PriskP: twelve closed bets: nine gains (about +$23) and THREE ZEROS in the 04:15 markets (about -$46). About -$23 as run. Six bets open in the 04:30 markets at the pass.

| Bet | Paid | Ended | Market | As run |
|---|---|---|---|---|
| SOL 04:00 YES 03:48:39 | 0.68 | early profit (stall) 0.78 | YES | +$1.75 |
| DOGE 04:00 YES 03:51:03 | 0.60 | fast 0.71 | YES | +$2.40 |
| DOGE 04:00 YES 03:52:35 (bought back) | 0.73 | top 0.99 | YES | +$5.60 |
| SOL 04:00 YES 03:53:08 (bought back) | 0.80 | sold 1 s later at 0.80 on the first bet's memory | YES | about -$0.25 (the fee) |
| SOL 04:00 YES 03:53:11 (bought a third time) | 0.80 | fast 0.84 | YES | +$0.35 |
| HYPE 04:00 NO | 0.77 | top 0.99 | NO | +$4.55 |
| BTC 04:15 YES 04:02:08 | 0.78 | NO SALE | NO | -$15.60 |
| BNB 04:15 YES 04:05:09 | 0.65 | early profit 0.72 | NO | +$0.95 (a save) |
| SOL 04:15 YES | 0.84 | top 0.99 | YES | +$2.70 |
| DOGE 04:15 YES 04:08:04 | 0.65 | early profit 0.74 | NO | +$1.15 (a save) |
| BNB 04:15 YES 04:09:10 (bought back 8 s after its own sale) | 0.73 | NO SALE | NO | -$15.20 |
| DOGE 04:15 YES 04:09:59 (bought back 3 s after its own sale) | 0.76 | NO SALE | NO | -$15.40 |
| ETH 04:30 YES | 0.58 | fast 0.67 | not in | +$1.45 |

The three zeros, traced on the bids:
- BTC 04:15 YES (0.78 at minute 2): dipped to 0.56, climbed to 0.92 at minute 9.7 (+12c after both fees, a real profit run), was 0.84 twenty seconds later and 0.58 a minute after, hovered 0.61 to 0.79, and went from 0.66 to 0 in the last forty seconds. The profit was not taken because the bet was two thirds of the way through its life, where early profit has faded to a tenth (the user's fade); its pace was 0.98, under the fast clause's 2. Under cost, the spot was never as far under the target as it had been over it until the settlement minute.
- BNB and DOGE 04:15 YES were BOUGHT BACK seconds after the rule had sold the same side at a profit because the bid had stopped climbing (BNB sold 0.72, bought 0.73 eight seconds later; DOGE sold 0.74, bought 0.76 three seconds later). Neither reached two wobbles of profit again; both fell to 0.39 within a minute, bounced, and went to 0 in the last forty seconds with the others.
- All three markets flipped in their final minute: a bid of 0.66 to 0.70 at 04:14:00 was 0.33 to 0.39 at 04:14:20 and 0 at 04:14:40. No exit of this rule acts on that.

What could have prevented them:
- BNB and DOGE: not buying the same side back seconds after the lane's own exit said the climb had stopped. Seen also on ETH 23:30 (sold 0.68, bought back 0.61, salvaged at 0.12) and SOL 04:00 above. It is entry logic (when a re-buy is allowed), so it is put to the user, not changed. Where the buy-back followed a sale and the market kept going (DOGE 04:00: sold 0.71, bought back 0.73, top at 0.99) it paid; tonight's count is one paid, three lost, one flat.
- BTC: only a profit that does not fade, which the replay of 00:20 priced at -$60 to -$90 on 210 bets. Not changed.

CHANGED (low risk, a bug): a bet that is sold out takes its watcher target with it. SOL 04:00's second bet was sold one second after its buy on the first bet's memory ("early profit: +8.3c" against the 0.68 the first bet paid), because the re-buy fix of 22:40 replaces the target only at the next 3 s refresh. `watcher._sell` now drops the target when the lane has no open bet left on the market; the end-to-end sale test checks it. Exit tests 152 passed. Deployed in the 04:30 gap.

wave_shape (the other session's lane): 67 closed bets, about -$17.30.

## Pass 9, 04:49 (bets bought 04:17 to 04:47)

Live build: b2480d4a since 04:30 (a sold bet takes its target with it). Full test suite on it: 1746 passed, 5 skipped.

PriskP: nine closed bets, seven gains and two losses, about +$9 as run. Two open at the pass.

| Bet | Paid | Ended | Market | As run |
|---|---|---|---|---|
| ETH 04:30 YES | 0.58 | fast 0.67 | YES | +$1.45 (about $9 more held) |
| DOGE 04:30 YES | 0.58 | top 0.99 | YES | +$5.70 |
| BTC 04:30 YES | 0.69 | top 0.99 | YES | +$6.30 |
| XRP 04:30 YES 04:17:44 | 0.70 | salvage 0.25 at 04:22:09 | YES | -$10.60 (held: +$6.60) |
| HYPE 04:30 NO 04:18:01 | 0.66 | fast 0.81 | YES | +$2.90 (a save) |
| ZEC 04:30 YES | 0.64 | top 1.00 | YES | +$8.30 |
| HYPE 04:30 NO 04:20:57 (bought back 19 s after its own sale, a steady-market buy at a peak) | 0.80 | no sale | YES | -$7.00 (8.7 contracts) |
| ZEC 04:45 YES | 0.84 | fast 0.90 | not in | +$1.00 |
| BNB 04:45 YES | 0.82 | fast 0.87 | not in | +$0.75 |

Noticed:
- XRP 04:30 YES: a salvage at 0.25 on a market that then settled YES. The loss side's cross (as far under the target as the spot had been over it in the market) passed: four and a half minutes into its life, the market's best cushion on the YES side had been small, so a small dip under counted. The weak case said to the user at 01:20 (a bet whose side never had much cushion still takes loss sales). One bet; with DOGE 02:45 (held to zero by the same test) the change stands one each way tonight. Not changed.
- HYPE 04:30 NO: the lane's fifth buy-back of the night within seconds of its own profit sale, and the fourth to lose its stake (paid once, flat once). The question is with the user since 04:19; not changed.
- ETH 04:30: the fast clause sold at 0.67 a bet that went to 1.00; the bid had stopped for a wobble 69 s after the buy. The price of the clause on a bet that only paused.

wave_shape (the other session's lane): 80 closed bets, about -$23.15.

Nothing changed this pass.

## Pass 10, 05:19 (bets bought 04:33 to 05:06)

Live build unchanged (b2480d4a).

PriskP: eight closed bets, six gains, one salvage and one zero, about -$6 as run.

| Bet | Paid | Ended | Market | As run |
|---|---|---|---|---|
| ZEC 04:45 YES | 0.84 | fast 0.90 | YES | +$1.00 |
| BNB 04:45 YES | 0.82 | fast 0.87 | NO | +$0.75 (a save) |
| NEAR 04:45 YES 04:35:54 | 0.83 | NO SALE | NO | -$15.20 |
| ETH 05:00 YES 04:47:07 | 0.59 | salvage 0.37 at 04:49:19 | NO | -$6.30 (held: -$14.75; the salvage was right) |
| DOGE 05:15 NO | 0.74 | top 0.99 | not in | +$5.00 |
| SOL 05:15 NO | 0.74 | early profit 0.77 | not in | +$0.30 |
| NEAR 05:15 NO | 0.75 | top 0.99 | not in | +$4.70 |
| SOL 05:15 NO (bought back 48 s after its own sale) | 0.80 | top 0.99 | not in | +$3.60 |

The zero, NEAR 04:45 YES: a steady-market buy at 0.83 (chance 0.82, cushion in the top fifth, note "3 of 4, off the peak, would buy too"). The bid stood at 0.90 to 0.92 from 04:38:00 to 04:39:20, about +7c after both fees, then slid to 0.83, 0.70, 0.51 and was 0.12 with two minutes left. No profit was taken: +7c is under two wobbles, so no profit run started, and the spot's cushion would have held it as well. Under cost the spot was not as far under the target as it had been over it until late.

A fix tried on the tape and NOT made: letting a stall take a profit of one wobble (a fall and the fast clause still needing two). On the 210 bets: +$89.64 against +$97.46 as live; the four extra stall sales were all on bets that went on to win. The same shape as DOGE 02:45 (+7c, then zero): two zeros tonight from a profit under two wobbles, and the replay still says taking those costs more than it saves. Left; counted.

The sixth buy-back after a profit sale (SOL 05:15 NO, bought back at 0.80 forty-eight seconds after selling at 0.77) paid: top at 0.99. Buy-backs tonight: two paid, four lost the stake, one flat.

wave_shape (the other session's lane): 96 closed bets, about -$22.50.

Nothing changed this pass.

## Pass 11, 05:49 (bets bought 05:20 to 05:41)

Live build unchanged (b2480d4a).

PriskP: five closed bets, three gains and TWO ZEROS, about -$23 as run. One open at the pass (ZEC 05:45 NO, 0.74, bought back 17 s after a fast sale at 0.71).

| Bet | Paid | Ended | Market | As run |
|---|---|---|---|---|
| BTC 05:30 NO 05:20:24 | 0.62 | NO SALE | YES | -$16.60 |
| ETH 05:30 NO 05:20:36 | 0.64 | NO SALE | YES | -$15.00 |
| BTC 05:45 NO | 0.83 | top 0.99 | not in | +$2.90 |
| ZEC 05:45 NO | 0.60 | fast 0.71 | not in | +$2.00 |
| BNB 05:45 YES | 0.80 | top 0.99 | not in | +$3.60 |

The two zeros, the same path on both markets: bought NO twelve seconds apart at minute 5.4; the bid fell to 0.27 and 0.33 by minute 9.7 (no loss sale: the spot was not as far under the target as it had been over it), came all the way back to 0.69 and 0.76 by minute 11 (BTC +4c, ETH +9c after both fees for about a minute: under or at two wobbles, late in the bets' lives, nothing taken), held 0.56 to 0.76 until ninety seconds from the close, and went to zero in the last minute.

That is the fourth and fifth zero of the night with this ending (BTC, BNB and DOGE 04:15 were the others): a bet near its cost with one or two minutes left, and the market decided against it in the settlement minute. The exits as they stand do nothing there by design: a profit that small is not taken, a late profit is not sold while the spot has not crossed, and a loss is not sold until the spot is well under. A late-market rule would be a new piece of logic, the kind the user asked to be shown, not built: put to the user with the count.

wave_shape (the other session's lane): 106 closed bets, about -$2.30 (it has recovered about $20 in the last half hour).

Nothing changed this pass.

## Pass 12, 06:19 (bets bought 05:37 to 06:02) and the night so far

Live build unchanged (b2480d4a).

This pass: five closed bets, four gains (two tops, two fast, about +$10) and one zero: ZEC 05:45 NO, bought back at 0.74 seventeen seconds after the fast clause sold the same side at 0.71; no sale, the market settled YES, -$15.20. The seventh buy-back after a profit sale tonight: two paid, five lost the stake, one flat.

THE NIGHT, PriskP live bets since the one rule went on (21:37), as run (the sale after its fee, or settlement, less the price paid; the buys rested):

| Stretch | What was live | Bets | Net | Gains | Zeros |
|---|---|---|---|---|---|
| 21:37 to 22:47 | the one rule, its loss sales dropped by the selling thread (my bug) | 13 | -$36.68 | 8 | 5 (-$74.56) |
| 22:47 to 01:00 | loss sales placed; the fade and two-wobble changes | 37 | -$93.75 | 24 | 2 (-$30.23) |
| 01:00 to 01:45 | the pullback wait on | 6 | +$23.46 | 6 | 0 |
| 01:45 to 06:15 | the rule as it stands | 57 | -$2.74 | 45 | 9 (-$130.29) |

Since 01:45, on the 56 bets whose markets have settled, by how the bet ended:

| Ended by | Bets | As run | Held to settlement | Settled the bet's way |
|---|---|---|---|---|
| the top (0.99) | 19 | +$87.92 | +$91.61 | 19 |
| profit gained too fast | 19 | +$40.22 | +$15.25 | 15 |
| early profit (fall or stall) | 7 | +$12.79 | -$21.71 | 4 |
| salvage | 2 | -$16.21 | -$8.14 | 1 |
| no sale, settled against | 9 | -$130.29 | -$130.29 | 0 |
| ALL | 56 | -$5.56 | -$53.27 | 39 |

What that says:
- The exits are earning their keep: the same 56 bets held to settlement would have made -$53.27; as run -$5.56. The fast clause and the early profit together took +$53 where holding those 26 bets would have lost $6.
- The bets themselves were not good enough: 39 of 56 settled the bet's way, 70%, at an average price paid of about 0.73. At that price a lane needs about 75% to break even before any exit. The user's reading ("It's an entry issue when they all fail like that. The exit failing was just the safety net") is what the numbers show.
- Nine bets were held to a total loss, about $14.50 each, against 45 gains of about $2.80 each. Five of the nine ended in the settlement minute from a bid near the cost (passes 8 and 11); three were bought back seconds after the lane's own profit sale.

Nothing changed this pass. For the user: (1) whether the lane may buy the same side back seconds after its own profit sale; (2) whether to design a late-market rule for a bet near its cost with a minute or two left; (3) the entries, which is where the night was lost.

wave_shape (the other session's lane): 119 closed bets, about +$9.

## Pass 13, 06:49 (bets bought 06:21 to 06:40)

Live build unchanged (b2480d4a).

PriskP: six closed bets, four gains and two losses, about -$15 as run.

| Bet | Paid | Ended | Market | As run |
|---|---|---|---|---|
| DOGE 06:30 YES | 0.78 | top 0.99 | YES | +$4.00 |
| XRP 06:30 YES 06:21:58 | 0.70 | fast 0.78 | NO | +$0.90 (a save) |
| BNB 06:30 YES | 0.75 | top 0.99 | YES | +$4.90 |
| XRP 06:30 YES 06:23:46 (bought back 16 s after its own sale, 1c higher) | 0.79 | NO SALE | NO | -$15.00 |
| NEAR 06:45 YES 06:34:02 | 0.61 | salvage 0.17 at 06:38:42 | not in | -$11.60 |
| NEAR 06:45 NO 06:40:22 (the other side, after the salvage) | 0.73 | fast 0.82 | not in | +$2.20 |

Noticed:
- XRP 06:30: the fast clause sold at 0.78 a bet whose market then lost, a whole stake saved, and sixteen seconds later the lane bought the same side back at 0.79 and lost that stake. The eighth buy-back of the same side within a minute of the lane's own profit sale tonight: two paid (+$9.20 together), six lost the stake (about -$90 together), one flat. It is the single most repeated loss of the night. It is entry logic and a new refusal, so by the rules of this watch it is not changed; it has been put to the user at 04:19, 04:49, 05:19 and 06:19 and is the first thing for them to decide.
- NEAR 06:45: a loss sale and then a buy of the OTHER side that paid: the re-buy the user described on 11-26 ("it could have rebought after the down trend and would have made money on both halves").

wave_shape (the other session's lane): 136 closed bets, about -$3.

Nothing changed this pass.

## The user's XRP 06:30 message (07:00 to 07:25): re-buys, and what the total losses look like

User, on XRP 06:30 YES (the second bet, bought at 0.79 at 06:23:46, sixteen seconds after the first was sold at 0.78; the bid went 0.81 to 0.19 in 75 s and to 0): "we are still getting a lot of these total losses"; then: "Looks like repeated buys don't have any delay after. But it also looks like it said yes to a repeated buy on a peak. Which we aren't supposed to buy on a peak. Why aren't repeat buys following the same logic?"

Findings, from the decisions and the tape:
1. PriskP's buy module never listed the `cooldown` step (the commodity lanes have had it since 11-19), so the lane did not remember its own sale. FIXED and deployed at 07:30: `cooldown` in `[module.priskp]` with `rebuy_wait_share` 0.1 (a tenth of the time the market had left at the sale; never fixed seconds). Same side waits after a sale for a gain; the other side goes through at once; a sale at a loss sets no wait.
2. The 12 settled same-side re-buys since 00:33: eight came within 20 s of their sale (two won, +$6.23; six lost, -$67.93), the four that came 48 s or more after it all won (+$13.41); net -$48.29. My earlier tallies in this log (two paid, five or six lost) were wrong; these are from the decisions table.
3. The peak: re-buys go through the same peak reading as any buy and every curve re-buy read "off the peak", because the reading is of the SPOT's swings and the quarter-size swing ran a little against the bet; the PRICE was the one the lane had just sold at (1c to 3c above it). Three steady-market re-buys read "at a peak" and the peak wait is a curve-only wait. Not changed; a reading of the bid's own peak is open.
4. The XRP bet itself: two gates failed. The loss side's cross test (as far under the target as the best cushion in the market) was set by a +16.9 spike at minute 2 that had faded before the buy and was reached at minute 12 with the bid at 0.01; and salvage waits for a bounce off the low, and this bid fell straight down, so only the time backstop (a third of the market, about 06:29:20) was left. The bid led the spot down, as on NEAR 00:00.
5. Total losses in general, on the 312 bets with tape since 11-25: of the 86 bets whose market went against them and whose bid fell under 0.05 at some point, the median fastest 60 s fall of the bid was 10.5 of its own wobbles; of the 68 that won the market after being 25c or more under the price paid, 6.6. A sale on a fast fall of 12 wobbles flags 33 of the 86 and 4 of the 68; of 14, 19 and 0. Replayed as a loss sale on the full set: a fall of 10 wobbles in 60 s sells five bets, all losers, at a median 0.10, for +$15 over 312 bets; 12 and 14 sell one bet each. The bid falls faster than any rule can read it: by the time a 60 s fall is 10 wobbles it is near zero. Not shipped: a fixed number of wobbles and a gain of about $0.05 a bet. The lever on total losses is the buy, not the exit.

Nothing else changed.

## Pass 14, 07:31 (bets bought 06:40 to 07:23)

Live build: the cooldown (re-buys of the same side wait a tenth of the time left after a profit sale) deployed at 07:30; before that b2480d4a. Not yet tested by a bet.

PriskP: nine bets in this stretch, eight closed at a gain (about +$18 as run: two sold at 0.99-class tops, six by the fast clause or early profit), none lost. One open at the pass: XRP 07:15 NO, bought 07:11:09 at 0.74, outcome not in yet.

Noticed:
- HYPE 07:15 NO was sold at 0.79 by the fast clause and bought back TWO SECONDS later at 0.78; the re-buy then sold at 0.93 (+$2.76). A re-buy that paid, before the cooldown existed; the night's count of same-side re-buys within a minute of the lane's own sale: 13 settled, seven won, six lost (about -$68 net of the winners' +$25).
- Two buys read "pass" (the steady-market path) at 0.78 and 0.83: both won.
- Nothing lost, so nothing to trace; nothing changed in this pass.

In progress and not in this pass: a lane sells only its own bets' contracts on its own side (the ZEC 05:45 cross-lane bleed; deploys after its full test run), and the curve entry's peak reading including the side's own price (branch price-peak).

## Pass 15 (07:23 to 07:49 ET)
Four live bets, all sold at a gain: HYPE 07:30 NO 0.64 to 0.76 (fast-profit clause); SOL 07:30 NO 0.80 to 0.92 (fast-profit clause, 0.038 off its high, cushion +1.19 sigma, the market then settled our way: left about $1.37, the user's "sold while price rising" message; proposed: the fast clause defers to the cushion hold, not yet replayed); ZEC 07:45 YES 0.71 to 0.997 (top); HYPE 07:45 NO 0.84 to 0.995 (top). Nothing changed this pass. Curve waits plus lane isolation merged on branch price-peak (3070afdb); full suite running, deploy at the next quarter-hour gap after it passes.

## ETH and ZEC 08:00 total losses, and three fixes replayed (07:50 to 08:15 ET)
ETH NO 0.84 held at +9 to +12c (bid 0.95 to 0.97, cushion 1.1 to 1.4 sigma) then lost it: the cushion hold on the profit side, then under cost with the spot not yet crossed (no loss), then the spot crossed and the bid went 0.67 to 0.10 in seconds. ZEC NO 0.66 never armed (+8c is under two of its 0.06 wobbles), then sat under cost with the spot not crossed until the bid was 0.31. Replays on the 312 live bets (11-25 to 11-27 07:23, no buy-back), baseline with the heartbeat +$29.01: (a) a fall of 2 wobbles let through the cushion hold +$15.29, 3 or more unchanged (does not touch ETH); (b) the fast stop as a stricter test lost to the current one; (c) releasing the no-loss hold in the last 15% / 25% / 33% of the market: +$25.58 / +$2.96 / -$70.14 on the 312 (the two new bets, out of sample, +$16.2 / +$17.5 / +$17.5): not robust, nothing shipped. The heartbeat (5% of the time left) was kept. Dated 08:15 ET, all against `scripts/priskp_fall_rule_replay.py`.

## The user's rule: bid under 50c, spot not crossed, no bounce or slow, sell (replayed 08:25 ET)
Replay on the 312 live bets (baseline with heartbeat +$29.01): a bid under 0.50 that is still making lows (no new low-free stretch longer than 5% of the time left, no bounce of a bar) with the spot not crossed: -$129.17 (126 sales, 61 beat hold, 65 lost), under 0.40 -$74.18, under 0.30 -$8.81. ETH 08:00 sold at 0.46 (-$7.09 against -$14.99), ZEC at 0.45 (-$4.94 against -$14.34), both at the 0.50 setting. Adding that the spot's cushion has shrunk by x sigma over three beats: x 0.1 -$92.28, 0.3 -$70.76, 0.45 -$28.54, 0.6 +$50.39, 0.8 +$6.37, 1.0 +$22.32; none of the settings that help the book sell ETH or ZEC, and all that sell them lose on it. Not shipped.

## Pass 16 (07:49 to 08:18 ET)
Two live bets, both sold at the top for a gain: BTC 08:15 NO 0.72 to 0.99 (+$5.7), HYPE 08:15 NO 0.84 to 0.992 (+$2.2). The ETH and ZEC 08:00 total losses are written up above. The heartbeat (fast profit sold on no new high for 5% of the time left) went live at 08:15:05 with the curve waits, lane isolation and the order-journal fix; the bot is up with no errors in its log. No fast-profit sale on the heartbeat yet to score. Nothing changed this pass.

## Pass 17 (08:18 to 08:49 ET)
One live bet: XRP 08:30 NO 0.66 to 0.914, the FIRST SALE ON THE HEARTBEAT (fast profit, +23.6c after fees, 113 s left, the bid at its high 0.92 for 5% of the time left with no new high, cushion +0.67 sigma); the market settled NO, so holding paid about $1.96 more on 22.7 contracts. That is the price of a stop on a fast climb that stalls and then finishes, the same trade the replay scored; one sale is not a verdict. Bot up, no errors in its log. Nothing changed.

## Pass 18 (08:49 to 09:19 ET)
No live bets since XRP 08:27 (about 50 minutes), with the user's price band 0.35 to 0.75 on the host (lane file pushed between 08:48 and 09:10; the host's notes read the band as 0.35 to 0.75 at 09:09:58). Refusals on PriskP's passes in the last 40 minutes by the entry's own words: check_not_passing 255, curve_not_held 250, curve_low_cushion 144, curve_peak 82, cushion_not_strong 14. The curve waits (low cushion, peak with the side's price) are acting as built; the expected cut was about 87% of curve buys (21 of 167 survive all three in the replay). Nothing changed. To score: the bets in the 0.35 to 0.75 band are a separate group from the 0.55 to 0.85 ones.

## Pass 19 (09:19 to 09:49 ET): is the dry spell the lane or the markets
No live bets since BTC 09:19 (sold 09:22 on early profit, 0.74 to 0.81; the dip re-buy was refused on the price band, now 0.35 to 0.90 on the host since about 09:46). Per half hour, PriskP's evaluated passes and the share the curve model's own verdict refused (`curve_wait`): 06:00 63%, 06:30 63%, 07:00 81%, 07:30 73%, 08:00 70%, 08:30 66%, 09:00 71%, 09:30 (half) 43%. The mix is the same before and after the 08:15 deploy, so the lane is not stricter; my three waits (not held, low cushion, peak) were 1,444 refusals in 08:00 and 428 to 479 since, the minority throughout. Nothing changed; the quiet is the markets' curve verdicts, not a fault. The five bets that the 07:00 half hour made (96 would-buy passes) are the exception, not the rate.

## Pass 20 (09:49 to 10:29 ET)
Live bets: ZEC 10:00 NO 0.78 to 0.992 (top, a gain); then XRP, HYPE and BNB 10:15 NO bought 10:06:03 to 10:06:44 at 0.85, 0.78 and 0.80 into a spot bounce off its 10:05:30 low, all three to zero (about -$42), no sale possible under the no-loss hold (the spot crossed at about 10:11 with the bids already at 0.24 to 0.32). Changed: the curve entry waits while the spot is bouncing back against the bet (`curve_bounce`, bounce 0.25 typical minutes off its best level since the swing began; the three read 0.52, 0.38, 0.66; on 318 bets under 0.1 u +$94.56, every higher bucket lost), deployed 10:28 ET at the user's word (build push, nothing open on an unclosed market). The user's price band is 0.35 to 0.90 (pushed about 09:46). CI: the ruff and mypy errors that failed every push since 11-26 are fixed (`05064664`); the runs on it are in progress. To score forward: every `curve_bounce` refusal and what the market then did.

## Pass 21 (10:29 to 10:49 ET)
One live bet so far: ETH 11:00 NO 0.72 bought 10:48:42, open at the pass. The bounce wait was reverted at 10:34 (the same reading measured from the swing's start reverses: the 36 buys it held back won 83% and held +$97.75); CI is green on `main` again (ruff, mypy, the suite with coverage). Another session pushed to the host at 10:45 from the local `main` (decision readings as data, a jump reading in shadow, the wave give-up as a share of the time left) and has 10 commits on local `main` not yet on GitHub; the bot is up with no errors in its log. Nothing changed by this pass.

## Pass 22 (10:49 to 11:19 ET)
Three live bets, all sold at a gain: ETH 11:00 NO 0.72 to 0.77 (early profit, +3.4c after fees), NEAR 11:00 NO 0.74 to 0.8156 (fast profit on the heartbeat, +8.8c, 436 s left, cushion +0.97 sigma; the market then went to 1.00, about 15c short of holding), BTC 11:15 NO 0.77 to 0.991 (the top, +$3.0). The user's question on NEAR: cushion and time should matter for the fast sale. Tally of the 26 fast sales since 01:45: cushion 1.0 sigma or more, 15 sales, 13 settled our way, holding minus sold -$7.70 (the two losers, SOL 02:36 at +1.74 and BNB 04:36 at +1.14, cost $35.00 to hold, with 518 and 529 s left); under 1.0 sigma, 11 sales, holding minus sold -$18.88. Scaling the heartbeat by the cushion factor 2 Phi(z) in replay: +$29.01 down to +$0.48 on 312 bets. Nothing separates the winners from the two collapses by cushion or time; nothing changed. Offered the user the buy-back through PriskP's curve check (the band is 0.35 to 0.90) as the other route.

## Pass 23 (11:19 to 11:49 ET)
Four live bets, all sold at the top for a gain: BTC 11:15 NO 0.77 to 0.991, DOGE 11:30 NO 0.69 to 0.995, XRP 11:45 YES 0.89 to 0.992, SOL 11:45 YES 0.89 to 0.993 (the last two bought at 0.89, the top of the 0.35 to 0.90 band). No losses. Changed nothing on the live host. Built and replayed on branch `exit-design` (not deployed): the fast early take inert in the last 90 s, the fast drop (a straight fall of the cushion toward the target in the last fifth of the market, sold for a small loss) and early take holding a fall while the cushion moves with curvature; +$91.75 on the 312 bets against the +$29.01 baseline (`studies/results/2026-11-27_exit_design_replay.txt`); awaiting the full suite and the user's word to deploy.

## Pass 24 (11:49 to 12:19 ET)
Four live bets: ZEC 12:00 NO 0.60 to 0.976 (top), NEAR 12:15 YES 0.67 to 0.99 (top), DOGE 12:15 YES 0.88 to 0.997 (top), and NEAR 12:00 YES 0.67 bought 11:52:18 with no sale recorded (held to settlement; the result and its bid trace were not readable at the pass, the host session timing out; to read next pass). Bot up 47 minutes, kill switch off. The exit design (`exit-design`, merged to local `main` as `f973dd85`: fast early take inert in the last 90 s, the fast drop, early take holding a fall while the cushion moves with curvature) was queued at the user's word for a build push at 12:30:05, after the 12:15 close, with nothing open.

## Pass 25 (12:19 to 12:49 ET)
Two live bets, both sold at a gain by the fast clause: BTC 12:30 NO 0.79 to 0.941 (+13.6c), BNB 12:45 NO 0.61 to 0.87 (+22.5c). NEAR 12:00 YES (bought 0.67, held, the nest said 4 of 4 and its own shadow said would wait) lost the full stake, about $14.77; the exit design that went live at 12:30:05 would have sold it at 0.20 (a -$10.61 loss) in replay. CI failed on the push at mypy only (five type errors in `oscillation.py` and `runner.py` from the other session's commits); fixed with a type annotation and a None guard, no change in behaviour. Bot up, kill switch off. Nothing else changed.

## Pass 26 (12:49 to 13:19 ET)
Two live bets, both sold at a gain by the fast clause: NEAR 13:00 YES 0.83 to 0.9376 (+9.5c), BTC 13:00 YES 0.77 to 0.964 (+18.0c). No losses, no `fast drop` sale yet since the exit design went live at 12:30:05. CI on `571c56ab` (the five mypy errors fixed) is still running. Nothing changed.

## Pass 27 (13:19 to 13:49 ET)
No live bets in the half hour (the last were NEAR and BTC 13:00 at 12:49 and 12:51). CI on `571c56ab` passed (ruff, mypy, the suite with coverage): CI is green on `main` again. No `fast drop` sale yet. Nothing changed.

## Pass 28 (13:49 to 14:19 ET)
Five live bets: DOGE 14:00 YES 0.76 to 0.9918 and ZEC 14:15 YES 0.86 to 0.995 (both the top, gains); then BTC 14:30 YES 0.77 (14:17:26), BNB 14:30 YES 0.88 (14:18:16) and ETH 14:30 YES 0.82 (14:18:22), three YES bets on three coins within a minute, open at the pass (the same shape as the three 10:06 NO zeros: one view of the market on several coins at once; no rule has been found for it that holds on the bets in hand). The bot restarted about 13:46 (up 32 minutes, kill switch off), most likely the other session's push of its wave-lane commit. No `fast drop` sale yet. Nothing changed.

## Pass 29 (14:19 to 14:49 ET)
The three correlated 14:30 YES bets all ended in gains: BTC 0.78 to 0.931 (fast clause, +13.7c), BNB 0.88 to 0.992 and ETH 0.82 to 0.99 (both the top). No new bets after them. No losses, no `fast drop` sale yet, nothing changed.

## Pass 30 (14:49 to 15:19 ET)
Five live bets, all NO, all sold at a gain: DOGE 15:00 0.88 to 0.972 (fast clause, +8.3c), NEAR 15:00 0.89 to 0.992 (top), BNB 15:00 0.89 to 0.99 (top), HYPE 15:15 0.81 to 0.9191 (fast clause, +9.5c), DOGE 15:15 0.83 to 0.991 (top). Three of them were bought at 0.89, the top of the 0.35 to 0.90 band. No losses, no `fast drop` sale yet. Bot up 2 hours, kill switch off, CI green. Nothing changed.

## Pass 31 (15:19 to 15:49 ET): the first fast drop sale, and it missed
One live bet: BNB 15:45 YES 0.82 bought 15:34:59 and SOLD BY THE FAST DROP at 0.30 at 15:42:01 (-54.5c a contract, about -$10.0 on 18.3 contracts): the spot's cushion was falling in a straight line and already -0.08 of a typical move under the target, with 178 s left. The bid had bottomed at 0.23 at 15:36:40, rebounded to 0.66, fell again to 0.34 / 0.25 / 0.11, and then ran 0.36, 0.77, 0.96, 1.00: the market settled YES, so holding paid about +$3.3, the sale cost about $13 against holding. It sold the bottom of a V. Replay on the 312 bets (baseline with the design +$91.75): requiring the cushion to still be over the target (not already under it) +$63.43, 37 sales instead of 56, so letting the fast drop fire once the cushion is at or under the target is worth $28 on the bets in hand; one live miss does not change that. Nothing changed; watching the next fast drop sales (this is the first, 1 of 1 lost to holding).

## Pass 32 (15:49 to 16:19 ET)
One live bet in the window: DOGE 16:00 NO 0.89 to 0.995 (top, a gain). No new bets after 15:51. The fast drop has fired once (BNB 15:45 YES, a miss against holding, about $13); no second sale. The bot restarted at about 16:01 (up 17 minutes, kill switch off), not by this session: the other session's push. Nothing changed.

## Pass 33 (16:19 to 16:49 ET)
One new live bet: HYPE 16:30 YES 0.83 bought 16:19:58, no sale (the bid ran 0.64 to 0.97 and then 1.00 at the close), held to settlement and won, about +$3.3. The fast drop is exempt from the loss-while-ahead veto since about 16:32 (the other session's push carried commit `dcb8aa76`; my 16:45:05 build push recreated nothing, the image unchanged); no fast drop sale since the exemption, so the count of fast drop sales is still 1 (BNB 15:45 YES, a miss, before the exemption). Bot up 17 minutes at the pass, kill switch off. Nothing changed.

## Pass 34 (16:49 to 17:19 ET)
Two live bets, both NO on the 17:15 market, both sold at a gain: XRP 0.88 to 0.9841 (fast clause, +9.8c) and NEAR 0.72 to 0.7725 (early profit, +2.3c after fees: the bid fell 0.100 from its high 0.87, over the 0.068 bar; the bid then went to 0.48 at 17:07, and ran to 0.98 and 1.00 at the close: holding paid about +$5.8 on the 20.8 contracts against about +$0.5 sold, a small gain that should have been more). Under the three concepts the cushion path or the size of the dip is the factor; one bet, nothing changed. The per-lane holding check (`risk.check(lane=...)`, so wave_shape's bets stop blocking PriskP) is committed and queued for the 17:30:05 build push once the full suite passes. No fast drop sale since the exemption. Bot up, kill switch off.

## Pass 35 (17:19 to 17:49 ET): the second fast drop sale, a second miss
Four live bets: BTC 17:30 YES 0.85 to 0.987 (top), ETH 17:45 NO 0.76 to 0.978 (top), XRP 17:45 NO 0.81 to 0.9287 (fast clause, +10.1c), and NEAR 17:45 YES 0.78 bought 17:40:47, SOLD BY THE FAST DROP at 0.4405 (-32.9c a contract, about -$6.4 on 19.5 contracts) with the veto exemption live (bot restarted 17:31): the bid ran 0.78, 0.75, 0.62 (17:41:30), 0.55, 0.26 (17:42:30), 0.46, 0.65, 0.81, 0.98 and 1.00 at 17:44:30, the market settled YES, holding paid about +$4.3 (+0.22 on the 19.5 contracts): about $10.7 against holding. Fast drop so far: 2 sales, both sold the bottom of a V that recovered (BNB 15:45 YES about -$13 against holding, before the exemption; NEAR 17:45 YES about -$10.7, with it): about -$23.7 against holding on 2 sales; the replay said 25 of 50 beat the rule without it. Not changed on two bets; the user decides whether to narrow it. Bot up 17 minutes at the pass, kill switch off.

## Pass 36 (17:49 to 18:19 ET): two more fast drop sales
Six live bets: ETH 18:15 YES 0.83 to 0.9889 (top), BNB 18:15 YES 0.83 to 0.97 (fast clause, +12.8c), BTC 18:15 YES 0.79 to 0.993 (top), and two fast drop sales. ETH 18:00 YES 0.89 sold at 0.31 at 17:59:04 (-58.2c, 16.9 contracts, about -$9.8): the bid was 0.96 at 17:58:00, 0.32 at 17:58:40, 0.44, 0.80 at 17:59:40 and 0.45 at 17:59:50, the result not recorded yet (holding paid about +$1.9 a contract-share if it settled YES, nothing if NO). SOL 18:15 YES 0.89 sold at 0.097 (-79.4c, about -$13.4) at about 18:12:20 after the bid fell 0.89 to 0.55 at 18:10:20 and 0.22 at 18:11:40: it settled NO (the bid was 0.00 from 18:14), so the sale saved about $1.6 against holding. Fast drop tally: 4 sales; BNB 15:45 YES about -$13 against holding (miss), NEAR 17:45 YES about -$10.7 (miss), SOL 18:15 YES about +$1.6 (late, saved a little), ETH 18:00 YES unknown until the result is recorded. Not changed; the user has the options (keep watching, restore the veto, switch it off) from pass 35. Bot up 47 minutes, kill switch off.

## Pass 37 (18:19 to 18:49 ET)
No new live bets after SOL 18:15 YES (bought 18:08). The loss-while-ahead veto is back on the fast drop, deployed 18:45:05 (suite 1799 passed, nothing open; bot up 2 minutes at the pass, kill switch off). The ETH 18:00 YES result (the fourth fast drop sale, sold at 0.31) was not readable at the pass (the host session returned nothing); fast drop tally unchanged: BNB 15:45 YES about -$13 and NEAR 17:45 YES about -$10.7 against holding, SOL 18:15 YES about +$1.6, ETH 18:00 YES pending. Nothing changed.

## Pass 38 (18:49 to 19:19 ET)
No live bets from SOL 18:15 (18:08) until XRP 19:30 NO 0.80 (19:17:43) and ZEC 19:30 YES 0.80 (19:18:35), both open at the pass: about 70 minutes with none (the veto went back on the fast drop at 18:45; the bot is up 32 minutes, kill switch off). The ETH 18:00 YES market closed at 1.00 (the bid 0.99 at 17:59:56 and 1.00 at 17:59:58): the fast drop sale at 0.31 missed, about $11.7 against holding on its 16.9 contracts. Fast drop tally, 4 sales: BNB 15:45 YES about -$13, NEAR 17:45 YES about -$10.7, ETH 18:00 YES about -$11.7 (all misses against holding), SOL 18:15 YES about +$1.6 (settled NO): about -$33.8 against holding, three of the four before the veto went back. Nothing changed.

## Pass 39 (19:19 to 19:49 ET): the fifth fast drop sale, the fourth miss
Eight live bets in the window. Gains: ETH 19:30 NO 0.86 to 0.978 (fast clause, +10.9c), XRP 19:45 YES 0.82 to 0.912 (fast clause, +7.4c), DOGE 19:45 YES 0.89 to 0.973 (fast clause, +7.4c). Open at the pass: HYPE 19:45 YES 0.87 (no sale, held to the close), ETH 20:00 NO 0.75, DOGE 20:00 NO 0.57. And ZEC 19:30 YES 0.80 (bought 19:18:35) SOLD BY THE FAST DROP at 0.24 (-58.4c a contract, about -$8.9 on 15.3 contracts) at about 19:29, with the veto on: the bid was 0.34 at 19:24, 0.13 at 19:25:40, 0.54 at 19:27:20, 0.08 at 19:29:00, then 0.67 at 19:29:20, 0.97 and 1.00 at the close: it settled YES, holding paid about +$3.1, the sale about $11.6 against holding. Fast drop tally, 5 sales: BNB 15:45 YES -$13, NEAR 17:45 YES -$10.7, ETH 18:00 YES -$11.7, ZEC 19:30 YES -$11.6 (all four YES bets that recovered), SOL 18:15 YES +$1.6: about -$45.4 against holding. Replay on the 58 bets newer than the settings (since 06:45): no fast drop +$54.27, the live version +$38.39, tighter versions +$34.7 to +$36.2: no setting beat off. Recommended to the user: switch it off (one constant); waiting for the word. Bot up about an hour, kill switch off. Nothing changed.

## Pass 40 (19:49 to 20:15 ET): AWS sign-in expired
The 20:15:05 deploy of the fast drop switch (`be49a011`, suite 1800 passed) did NOT go out: "Error when retrieving token from sso: Token has expired and refresh failed". Every host call (status, SQL, the push) fails until the user signs in to AWS again. The bot is still running the previous build (the fast drop on, the loss-while-ahead veto on it), with open bets from the last pass (HYPE 19:45 YES, ETH 20:00 NO, DOGE 20:00 NO). Per the standing instruction, no further changes are made until the sign-in is renewed. The fast drop tally stands at 5 sales, about -$45.4 against holding.

## Pass 41 to 42 (20:15 to 20:49 ET): AWS sign-in renewed
The user renewed the sign-in at about 20:45. The bot had been up 2 hours throughout (kill switch off) and had kept betting while I could not read it: ETH 20:00 NO 0.75 to 0.968 (fast clause, +20.6c), DOGE 20:00 NO 0.57 to 0.74 (fast clause, +13.9c), BTC 20:00 NO (stake $0.83, closed by hand: the account no longer held the contracts, booked at 0.972), ETH 20:15 YES 0.75 to 0.919 (fast clause, +15.1c), BTC 20:15 YES 0.71 to 0.97 (fast clause, +24.4c), HYPE 20:15 YES (stake $0.96, closed by hand at 0.931), SOL 20:45 YES 0.71 to 0.75 (early profit, +3.3c, the bid fell 0.050 from its high), HYPE 20:45 YES 0.88 to 0.991 (top). No losses and no fast drop sale since 19:29. The user had not seen bets "for several hours": the bets are short, bought and sold within minutes, so an account view of open positions shows none most of the time. The fast drop switch (`be49a011`, suite 1800 passed) was queued for the 21:00:05 build push, nothing open.

## Pass 43 (20:49 to 21:20 ET)
The fast drop switch went out at 21:00:05 (nothing open; bot up 19 minutes at the pass, kill switch off). Bets since 20:36: BNB 21:15 YES 0.75 to 0.99 (top, gain); BTC 21:00 NO 0.58 bought 20:51:43 SOLD BY SALVAGE (the old rule, not the fast drop) at 0.31 at about 20:53:30 (-31.2c a contract, about -$8.1 on 25.9 contracts): the NO bid was 0.45 at 20:52, 0.29 to 0.31 from 20:53 to 20:54, 0.43 to 0.50 later, 0.63 at 20:58:30, 0.89 at 20:59:30 and 1.00 at the close: the market settled NO, so holding paid about +$10.9: about $19 against holding (a V that recovered, like the fast drop misses; salvage's record in the replay was +$20 against the old exits on 13 sales). ETH 21:15 NO 0.65 held to the close (result to read); ZEC 21:30 YES 0.85 open. Nothing changed. The entry waits replayed on refusals in markets PriskP never bought (since 08:20): low cushion about -$147 after fees (right to refuse), peak about -$11 (break-even), not held about -$79: not loosened.

## Pass 44 (21:49 to 22:19 ET)
Live PriskP bets since 21:30: ZEC 21:45 NO 0.86 to 0.992 (top, gain), XRP 22:00 YES 0.73 (21:52:33, 20.5 contracts) HELD TO ZERO (-$14.99), HYPE 22:15 YES 0.86 to 0.99 (the top rule, 80 s left, +13c: not the fast clause). XRP 22:00 YES: the bid ran to 0.93 (+17c) and whipsawed 0.78 to 0.92; the rule read "cushion" every second from 21:55:20 to 21:56:30, held by the rising half of the curvature hold (z +0.5 to +0.97, under the 1.0 line) through falls of 11 to 15c; then the cushion fell, the bid went under cost, no sale allowed (the spot had not crossed) and it settled 0. Replay on 382 bets: curved-only hold +$92.56 against today's +$50.98 (no hold +$84.82); that bet sold at 0.78 under it. Changed: the curvature hold now applies only to a curved approach (`b3705cd0`, suite queued, deploy at the next gap). Also found and fixed earlier in the evening: a lane buying the other side of a market another lane holds got netted by the exchange (BTC 20:00 NO 1.0 contract, HYPE 20:15 YES 1.2, ZEC 21:30 YES 5.0, booked "closed by hand"); the wave lane held the XRP 22:00 opposite side too (NO 5.3 closed by hand when PriskP's YES netted it). The opposite-side block went live at 22:00:05 (suite 1802 passed). The bot restarted at about 22:16 (up 2 minutes at the pass, kill switch off), likely the other session's push carrying the curvature commit; the queued 22:30 build would be a no-op if so.

## Pass 45 (22:19 to 22:49 ET)
Two live bets, both NO on the 22:30 market, both gains: SOL 0.89 to 0.99 (top, 169 s left), NEAR 0.84 to 0.916 (fast clause, +6.4c). No bets after 22:24:41 (about 25 minutes). No losses. The curved-only curvature hold, the salvage gate and the opposite-side block are live (the bot restarted at about 22:16, up 32 minutes at the pass, kill switch off); no early-profit fall sale on a rising cushion yet to confirm the 22:16 build carries the curvature change. Nothing changed.

## Pass 46 (22:49 to 23:19 ET)
No live PriskP bets since NEAR 22:30 NO (22:24:41), about 55 minutes. PriskP kept evaluating (passes since 22:25 by reason: dead_ask_band 6166, window 1569, curve_not_held 662, check_not_passing 441, curve_peak 265, curve_low_cushion 173, shift_out_of_cushion 109), the wave lane made 31 bets in the same time: the markets sit near 50/50 (the ask in the 0.45 to 0.55 dead band), where a favourites lane has nothing to buy; not a fault. The bot restarted at about 23:16 (up 2 minutes at the pass, kill switch off), not by this session. No losses, nothing changed.

## Pass 47 (23:19 to 23:49 ET)
Three live bets: HYPE 23:45 NO 0.59 to 0.86 (fast clause, +22.4c), ZEC 23:45 YES 0.83 to 0.993 (top, 64 s left), and HYPE 00:00 NO 0.86 bought 23:48:51, open at the pass. No losses; the quiet after 22:24 ended at 23:35. Bot up 32 minutes, kill switch off. Nothing changed.

## Pass 48 (23:49 to 00:19 ET, 2026-11-28)
Three live bets, all gains: HYPE 00:00 NO 0.86 to 0.9687 (fast clause, +10.0c), XRP 00:00 YES 0.89 to 0.995 (top, 42 s left), XRP 00:15 NO 0.82 to 0.9667 (fast clause, +13.7c). No losses, nothing open at the pass. Bot up about an hour, kill switch off. Nothing changed.

## Pass 49 (00:19 to 00:49 ET, 2026-11-28)
Five live bets, all NO, all gains: DOGE 00:30 0.75 to 0.80 (early profit, +0.5c after fees, the bid fell 0.138 from its high 0.92), BTC 00:45 0.85 to 0.992 (top), ETH 00:45 0.86 to 0.99 (top), SOL 00:45 0.84 to 0.98 (fast clause, +12.9c), DOGE 00:45 0.87 (a $5.44 stake) to 0.9826 (fast clause, +10.5c). The DOGE 00:30 sale is a small gain that should have been more: the NO bid ran 0.75, 0.88, 0.65, 0.85, 0.80, 0.77, 0.65, 0.52 (00:28:15), 0.91 (00:29:00) and 1.00 at the close; the market settled NO, holding paid about +$4.9 against about +$0.1 sold; a whipsawing bid (moves of 0.15 to 0.25 inside 15 s); one bet, nothing changed (the early-profit fall bar and the curved-only hold are the factors; no replay change is supported by one bet). Bot up 2 hours, kill switch off.

## Pass 50 (00:49 to 01:19 ET, 2026-11-28): a total loss
One live bet in the half hour: ETH 01:15 NO 0.85 (stake $14.82, bought 01:10:04, four minutes in), no sale, held to the close and LOST THE STAKE (about -$14.82). The NO bid (1 - ask) was 0.72 at 01:11:00 (under the price paid from the start), 0.71, 0.59, 0.28 at 01:12:00, 0.18, 0.49, 0.39, 0.28, 0.28, 0.52 at 01:14:00, 0.26, 0.01 at 01:14:40 and 0 at the close. Never ahead after fees, so no profit rule applied; under cost the no-loss hold and the salvage (which now needs the spot a full typical move of the time left under the target, `SALVAGE_Z`, and a failed bounce) sold nothing before the close, with only about five minutes left when it fell. The buy note, the strike and the market result were not readable at the pass (the host session returned nothing and the result is not recorded yet): the replay trace is for the next pass. No change on one bet; this is the shape of every earlier zero (an entry at 0.85 that never goes ahead), and the salvage gate is the factor to check against it. Bot up 2 hours, kill switch off.

## Pass 51 (01:19 to 01:49 ET): the ETH 01:15 NO zero traced
Replay of ETH 01:15 NO (bought 0.85 at 01:10:04 with 290 s left): with the salvage gate (SALVAGE_Z -1.0) AND without it (99) the rule sells nothing: settled lost, -$14.82 both ways. The reading every 20 s: "no cross" the whole time, the spot's cushion z +0.67 at 01:10:10 falling to +0.04 at 01:12:10 and between -0.08 and -0.39 from 01:12:30 to the close, never the cross the no-loss hold needs (a wobble of the spot under the target AND as far under as it had been over); the NO bid went 0.77, 0.67, 0.47 (01:11:50), 0.18 (01:12:10), 0.61 (01:12:50), 0.29, 0.52, 0.02 (01:14:30). So the salvage gate is not the cause; it is the no-loss hold with the spot hovering at the target for the last three minutes while the bid whipsawed. Same shape as the earlier zeros (an entry near 0.85 that is never ahead). Nothing changed. Other bets: DOGE 01:30 YES 0.86 to 0.931 (fast clause, +6.8c), BNB 01:30 YES 0.87 to 0.991 (top), HYPE 01:30 YES 0.89 held to the close and won (+$2.0). Bot up 3 hours, kill switch off.

## Pass 52 (01:49 to 02:19 ET): a second zero, and the price bands scored
Two live bets: DOGE 02:00 YES 0.85 to 0.992 (top, gain) and XRP 02:00 YES 0.87 (bought 01:50:56, $15.17), no sale, LOST THE STAKE (about -$15.17): the YES bid 0.90 at 01:52:00, 0.83, 0.86, 0.64 (01:53:30), 0.31 (01:54:00), 0.41, 0.25, 0.31, 0.15, 0.28, 0.19, 0.05 (01:58:30), 0 at the close; never meaningfully ahead (the peak 0.90 was about +2c after fees, under the profit arm), so only the no-loss hold and the salvage applied. Two zeros in an hour (ETH 01:15 NO 0.85, XRP 02:00 YES 0.87), both entries at 0.85 to 0.87 that reversed. PriskP bets since the user's 0.35 to 0.90 band (09:46), settled or sold, excluding the tiny netted ones, by price paid, as run: under 0.65: 5 bets, 4 up, +$15.69; 0.65 to 0.75: 10 bets, 7 up, -$22.59; 0.75 to 0.85: 34 bets, 27 up, -$9.44; 0.85 and over: 30 bets, 27 up, +$23.68. So the high-price entries are the best bucket since the band change, the zeros are its tail; the band is not changed. Bot up 3 hours, kill switch off. Nothing changed.

## Pass 53 (02:19 to 02:49 ET)
Five live bets, all gains: ZEC 02:30 NO 0.83 to 0.921 (fast clause, +7.8c), ETH 02:30 YES 0.60 to 0.993 (top), XRP 02:45 YES 0.79 to 0.84 (early profit, +2.9c after fees, the bid fell 0.070 from its high 0.91 at 02:37:30; the bid then dipped to 0.63 at 02:40:30 and ran to 1.00, the market settled YES: holding paid about +$4.0 against about +$0.5 sold: a small gain that should have been more), SOL 02:45 YES 0.79 to 0.88 (fast clause, +7.1c), DOGE 02:45 YES 0.73 to 0.996 (top). Sale kinds since 12:30 (sold against held to settlement, the markets with results, bets over 2 contracts): early profit 4 sales, all 4 won, sold +$4.01 against held +$20.64; fast clause 24 sales, 23 won, +$61.24 against +$66.93; top 25, all won, +$66.15 against +$70.21; the six 'other' were the five fast drop sales and the ZEC netting booking (-$50.42 sold against -$1.77 held, the fast drop is off). The four early-profit sales are the small gains that should have been more, but the replay on 382 bets (today's hold) has the early-profit fall sale at +$71.97 as run on 59 sales, and requiring a heartbeat before it falls to +$20.37 on 17 (total +$92.56 to +$27.72): not changed. Bot up 4 hours, kill switch off.

## Pass 54 (02:49 to 03:19 ET)
Four live bets on the 03:15 market, all NO, all gains: ETH 0.77 to 0.81 (early profit, +1.7c after fees, the bid fell 0.080 from its high 0.89: another small early-profit sale), ZEC 0.75 to 0.85 (fast clause, +6.7c) AND THEN A RE-BUY OF THE SAME SIDE: ZEC NO 0.85 (a $9.61 stake, bought at 03:05:18 after the fast clause sale at 03:02:32, about 2 minutes 45 seconds later and at the price it had just sold at) sold at 0.991 (top): the first buy-back through the normal evaluation that paid, +$1.4 on 11.3 contracts; HYPE 0.78 to 0.9037 (fast clause, +10.7c). No losses. Bot up 4 hours, kill switch off. Nothing changed.

## Pass 55 (03:19 to 03:49 ET)
Three live bets on the 03:45 market, all NO, all gains: DOGE 0.84 to 0.95 (fast clause, +9.7c), SOL 0.81 to 0.989 (top), NEAR 0.82 held to the close and won (the bid 0.78 to 0.69 at 03:38, 0.94 at 03:39:30, 0.99 at 03:41, 1.00: about +$3.3). No losses. Bot up 5 hours, kill switch off. Nothing changed.

## Pass 56 (03:49 to 04:19 ET)
One live bet: SOL 04:00 NO 0.74 to 0.85 (fast clause, +8.8c). No losses; a quiet half hour. Bot up 5 hours, kill switch off. Nothing changed.

## Pass 57 (04:19 to 04:49 ET)
Four live bets, all NO, all sold at the top for a gain: DOGE 04:30 0.69 to 0.993, NEAR 04:30 0.82 to 0.992, ETH 04:45 0.88 to 0.99, DOGE 04:45 0.79 to 0.99. No losses. Bot up 6 hours, kill switch off. Nothing changed.

## Pass 58 (04:49 to 05:19 ET)
Three gains and two open: SOL 05:00 YES 0.82 to 0.962 (fast clause, +12.9c), ETH 05:00 YES 0.88 to 0.965 (fast clause, +7.4c), ZEC 05:15 NO 0.84 to 0.99 (top); open at the pass: BNB 05:30 YES 0.74 (05:17:18) and HYPE 05:30 YES 0.64 (05:17:39), two YES bets on two coins 21 seconds apart. No losses. Bot up 6 hours, kill switch off. Nothing changed.

## Pass 59 (05:19 to 05:49 ET)
HYPE 05:30 YES 0.64 to 0.994 (top, gain) and HYPE 05:45 NO 0.82 to 0.983 (fast clause, +17.0c) were sold. Two bets had no sale and were held to their close, results not readable at the pass (the host query timed out): BNB 05:30 YES 0.74 (05:17:18, $15.00) and ZEC 05:30 NO 0.89 (05:19:10, $15.00): to read next pass. No other bets. Bot up 7 hours, kill switch off. Nothing changed.
