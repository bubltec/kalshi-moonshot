# Binary risk model: every reading, exactly (for the user's audit, 2026-11-20)

Source: `src/kalshi_moonshot/binaryrisk.py` (`readings()`, `side_vector()`, `NAMES`). 30 inputs per side. A coin reads Kalshi's settlement index, a commodity reads Kalshi's perp reference (`perpref.py`, S&P excluded). Ticks are about one a second. `now` is the moment of the decision, `left` is the time to the close in minutes.

**The unit, the typical minute `u15`:** root-mean-square of the one-second log moves over the last 15 minutes, times sqrt(60). Every move below is divided by it, so it is in this market's own typical moves. `uh` is the same over the last hour.
**The strike:** a coin's market strike; a commodity's perp value at the window's open (first tick within 25 s).
**The spot vector:** `sp1`, `sp3`, `sp5` are the plain averages of all ticks in the last 1, 3 and 5 minutes (each window ends at `now`; they overlap). `blend` = (sp1 + sp3 + sp5) / 3.
**Side view:** a NO side flips the sign of the direction readings and turns a place `p` into `1 - p`.

| # | Name | Definition | Window | Units / range | Side view |
|---|---|---|---|---|---|
| 1 | `logit_ask` | ln(ask / (1 - ask)), ask clipped to 0.02-0.98 | now | log-odds | the side's own ask |
| 2 | `ask_off` | 10 x (ask - 0.5)^2 | now | 0 at 0.5, grows to the ends | side's ask |
| 3-5 | `cz1`, `cz3`, `cz5` | ln(sp_m / strike) / (u15 x sqrt(left)), clipped to +-8 | m = 1, 3, 5 min averages | typical moves left in the time left | x sign (YES +, NO -) |
| 6 | `tr13` | ln(sp1 / sp3) / u15, clipped +-30 | 1 vs 3 min averages | typical minutes; positive = rising | x sign |
| 7 | `tr35` | ln(sp3 / sp5) / u15, clipped +-30 | 3 vs 5 min averages | typical minutes | x sign |
| 8 | `left` | time to the close in minutes (at least 1 s) | now | minutes | same |
| 9-11 | `czl1`, `czl3`, `czl5` | cz_m x min(left, 15) / 15 | as cz | the cushion times the share of the market left | x sign |
| 12 | `vr` | u15 / uh - 1 | 15 min vs 1 h typical minute | ratio; positive = the market has sped up | same (side-neutral) |
| 13 | `amp15` | ln(highest tick / lowest tick over 15 min) / u15 | last 15 min | typical minutes | same (side-neutral) |
| 14-16 | `pos15`, `pos30`, `pos60` | (blend - lowest tick) / (highest - lowest) over the window | last 15, 30, 60 min | 0 = the low, 1 = the high (0.5 if flat) | place p becomes 1 - p for NO |
| 17-19 | `mv15`, `mv30`, `mv60` | ln(blend / the first tick of the window) / u15, clipped +-60 | 15, 30, 60 min | typical minutes | x sign |
| 20-22 | `sl15`, `sl30`, `sl60` | wave shape, slope: the window (last 15/30/60 min) is cut into 12 equal bins, each bin's mean spot is taken, y = ln(bin mean / the last bin's mean) / u15, a straight line is fitted by least squares on x in [-1, 1] (-1 the window's start, +1 now); the slope is the line's rise per half-window | 15, 30, 60 min | typical moves; positive = rising | x sign |
| 23-25 | `cv15`, `cv30`, `cv60` | wave shape, curvature: the same fit with a parabola; its x-squared term (centred) | 15, 30, 60 min | typical moves; positive = bending up (accelerating), negative = flattening or a hump (log-like, sine top) | x sign |
| 26-28 | `ft15`, `ft30`, `ft60` | wave shape, line fit: R-squared of the straight line on the 12 bins (1 = a pure line, low = a wave; flat window reads 1) | 15, 30, 60 min | 0 to 1 | same (side-neutral) |
| 29-30 | `dr24`, `dr72` | ln(blend / spot 24 h or 72 h ago) / (u15 x sqrt(minutes in 24 h or 72 h)), clipped +-8 | 24 h, 72 h | the move against what a random walk of this market's typical minute would move; 0 when the reference is not yet known | x sign |

**References for `dr24`, `dr72`:** live, a background job (`refresh_refs`, every 10 minutes, for each index a decision has asked about) reads the last tick at or before 24 h / 72 h ago (within 30 minutes) from the tape into memory; a decision reads memory only. In training, the same lookups are made at each market's open.
**Needs:** at least 15 minutes of ticks (over 300), at least an hour (over 1,500), a last tick within 10 s of `now`.

**What the model does with them:** one ridge regression per exit (held to settlement; sold at the bid 1, 2, 3, 5 minutes later) on the standardised readings, target = net profit per $1 after the fee (capped at +3 in the fit), refit hourly on the last 24 h of settled markets sampled every 60 s, both sides. The prediction is capped at what a bet can pay, (1 - ask - fee) / ask. A side is bought when its best exit's predicted net reaches the bar, the top 5% of the training predictions (never an ask 0.45-0.55 or under 0.20).

**`swing_risk` (the early swing lane's last step):** the same prediction, kept per market series for the last 3 hours (one per market and side every 30 s); buy only when the best exit is an early sale (not held) and the prediction is in the top 3% of that series' own predictions (needs 200).

**Removed:** the recent YES rate (settlement outcomes), efficiency, the room up and down, the hour's place and move, the strike's place in the hour, the unscaled cushion `zt*`. Crossings was never added.
