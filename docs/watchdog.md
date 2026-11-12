# Watchdog: an email when the live bot stops (proposal, 2026-11-12)

**Why.** On 2026-11-12 the cloud host lost its network for about 11 hours (SSM ConnectionLost 01:43 ET, EC2 status impaired). No bets from 01:37 to 13:04 ET and nobody was told.

**Design.** Alarms live outside the host, so they fire when the host cannot speak.

| Alarm | Signal | Fires after | Catches |
|---|---|---|---|
| `HostStatus` | EC2 `StatusCheckFailed` (instance + system), 2 of 3 minutes | about 2-3 min | lost network, kernel crash, dead hardware (11-12's failure) |
| `BotHeartbeat` | custom metric `KalshiMoonshot/Bot Heartbeat`, 5 of 5 one-minute points missing, missing = bad | about 5-6 min | stopped container, hung worker, no route to AWS/Kalshi, host gone |

Both notify one SNS topic (email), and also on recovery.

The heartbeat (`src/kalshi_moonshot/heartbeat.py`) sends one datapoint a minute only while the bot is making progress: a job finished or the quote stream delivered an event in the last 5 minutes. A bot that is up but doing nothing sends nothing and alarms. It does not look at bets, since a quiet market legitimately has none; it looks at the machinery that would place them.

Not covered: a bot that works but whose rules decline every bet (that is a study, not an outage).

## Files
- `infra/watchdog_stack.py`: new self-contained stack `KalshiMoonshotWatchdog` (like JobsStack; does not reference `KalshiMoonshotBot`, so no instance-replacement drift risk). Adds one policy to the host role: `cloudwatch:PutMetricData`, only for the `KalshiMoonshot/Bot` namespace.
- `src/kalshi_moonshot/heartbeat.py`, wired in `worker.py`; settings `HEARTBEAT_ENABLED` (default off), `HEARTBEAT_REGION` (default us-east-2).
- `infra/compose.cloud.yml` is deliberately not changed (it is baked into the instance's user data, so editing it would replace the instance). The flag goes in the env file instead.

## Apply (user does this; nothing here was deployed)
1. Find the host role name: `aws ec2 describe-instances --region us-east-2 --instance-ids i-0c9a816da43ec969d --query 'Reservations[0].Instances[0].IamInstanceProfile.Arn'`, then `aws iam get-instance-profile` for the role.
2. `cd infra && cdk diff KalshiMoonshotWatchdog -c alert_email=YOU -c bot_role_name=ROLE --profile <sso>`; then `cdk deploy` that stack only. Click the confirmation link AWS emails. Alarms work now for the host status check; the heartbeat alarm will fire until step 3 (expected: it is the test that email works).
3. Add `HEARTBEAT_ENABLED=true` to the SSM dotenv parameter `/kalshi-moonshot/dotenv`, then ship the image with `scripts/cloud_push.sh --build` at about xx:44:40 / xx:14:40 (this restarts the bot). The user's call; no live setting was touched here.
4. Test: `docker stop` the bot container (or block its egress) and expect mail in about 6 minutes; start it and expect the OK mail.

Cost: two alarms and one custom metric, about $0.40 a month.
