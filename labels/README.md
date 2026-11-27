# Bet labels: the user's corrections of what the automation did

One line per correction in `bet_labels.jsonl`. This is the record the facts board's logic is checked against and, later, the training data for a model (user, 2026-11-27: "My hand trades are horrible. I need to see what the automation does and correct it so we can have a fact board that is really a logic model. And we can start training a real ai model on this data").

A line holds: the bet (`decision_id`, `ticker`, `lane`, `side`, `ts_et`, `ask`, `exit_price`, `exit_kind`), the user's own words (`said`, quoted), the label in a few fixed words (`label`), which part of the logic it corrects (`part`: buy, sell, both, books), and `by` (always the user: a label is never inferred by Claude).

Labels so far: `no_wave` (bought where the market had made no wave), `spike` (the move was a spike, not a wave's dip or peak), `sold_too_early` (the sale left before the move the bet was for), `sold_at_the_low` (a loss sale at the bottom of a dip that then came back), `late_buy` (the buy came after the turn had begun), `rebuy_no_logic` (a re-buy with nothing about what the lane had just done).

The readings at each bet (the facts) are not copied here: they are in the bet's own row on the host (`decisions.reason`, `factors`), joined by `decision_id`.
