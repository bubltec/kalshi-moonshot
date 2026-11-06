# kalshi-moonshot: rules for studies, rules and changes

## How we study and decide (applies everywhere in this repo)

1. **All buying and selling logic is based on curve variance.** Every entry, exit, stake and gate reads how the market's own curve is behaving, in units of its own variance (typical minutes: the root-mean-square one-second move times sqrt(60)), not in absolute cents, basis points or dollars. **Classify by curve, never by asset.** No rule, gate, split or study may be keyed on a coin, commodity or market name. A market is described by what its curve is doing: the measures in `src/kalshi_moonshot/marketprofile.py` (typical-minute size, crossings, range, late flips, path efficiency) and, per bet, the spot's path efficiency, bounce and drift over the minutes before the decision. Asset names may label a log line or a report, nothing more. When a result looks like "it works on crypto", the cause is a curve shape; find the shape.
2. **Thresholds come from the market's own recent history.** A cutoff is a quantile of that market's last hours of readings, recomputed on a schedule (hourly for ticks in memory, daily for the tape). A fixed number is allowed only for something structural (the fee, the close time, a settlement window).
3. **Anomalies are their own state.** A reading outside its market's recent range (beyond its 1st/99th percentile, or volatility over three times its median) is logged as `anomaly`, is never counted as choppy or straight, and no rule is assumed to hold there. The rule sits out and the reading is recorded so the state can be studied.
4. **A study asks one question on the same bets.** Compare against what actually happened on those bets (held to settlement, or the lane's own exit). Include the fee on every leg. Report n, the net change in dollars, how many bets the rule beat and lost to what happened, and the result in two halves of the period and across curve shapes. A rule needs a positive result in both halves and in each curve shape it claims before it goes to shadow. Settings chosen after seeing the results are said to be so.
5. **Shadow first, then live.** A new rule logs "would do X" with its curve reading, is scored against the bets that followed, and runs for at least a week of live bets before anyone turns it on. The live switch is the user's. Paper before real money.
6. **Rules retire.** Every shadow rule keeps a rolling score against what happened. A rule whose score turns negative over its window is flagged in the log and stops being proposed.
7. **Keep the evidence.** Scripts in `scripts/`, results in `studies/results/` (dated), the question and the answer in the commit message. A claim made in chat is backed by a file here or it is marked as unverified.

## Working rules

- Query the Docker Postgres, not `data/moonshot.db` (stale). Live runs in the cloud (us-east-2 host through SSM); the Mac runs config and analysis.
- `data/strategies.toml` is untracked and shipped by `scripts/cloud_push.sh`. A config that names a new lane field needs the new image: use `--build`. An old image refuses an unknown field.
- A restart (`--build`) lands just after a quarter-hour close: start the push at about xx:44:40, xx:14:40 and so on, after a warm build.
- Do not add attribution lines to commit messages or pull request descriptions.
