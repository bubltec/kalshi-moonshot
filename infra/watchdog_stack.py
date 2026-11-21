"""A watchdog outside the bot host: an email when the live bot stops working (2026-11-12).

On 2026-11-12 the host lost its network for about 11 hours (EC2 status checks impaired, SSM ConnectionLost at 01:43 ET). The bot placed no bets and nothing told anyone. Two alarms, one email topic:

  * HostStatus: the EC2 status checks (instance and system) failing for 2 of 3 minutes. Catches a lost network, a crashed kernel or dead hardware. EC2 sends these itself, so it works when the host cannot.
  * BotHeartbeat: the bot puts KalshiMoonshot/Bot Heartbeat every minute while it is making progress (src/kalshi_moonshot/heartbeat.py). 5 of 5 minutes with no datapoint, missing counted as bad,
    fires about 5-6 minutes after it goes quiet. Catches what the status checks cannot: a stopped container, a hung worker, a lost route to Kalshi or AWS APIs.

Self-contained, like JobsStack: it does not reference BotStack (whose template has drifted: a newer machine image would replace the instance), so deploying it cannot touch the live host. It needs two
context values: bot_instance_id (the InstanceId output of KalshiMoonshotBot) and bot_role_name (the host role's name; find it with `aws ssm describe-instance-information` / the instance profile), plus alert_email.
The role gets one extra policy: cloudwatch:PutMetricData on the KalshiMoonshot/Bot namespace only. The bot sends nothing until HEARTBEAT_ENABLED=true is in its env (the SSM dotenv parameter) and the new image runs.
"""
from __future__ import annotations

from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

NAMESPACE = "KalshiMoonshot/Bot"
METRIC = "Heartbeat"


class WatchdogStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = self.node.try_get_context
        instance_id = str(ctx("bot_instance_id") or "i-0c9a816da43ec969d")
        role_name = str(ctx("bot_role_name") or "")
        email = str(ctx("alert_email") or "")

        topic = sns.Topic(self, "Alerts", display_name="kalshi-moonshot watchdog")
        if email:
            topic.add_subscription(subs.EmailSubscription(email))  # AWS emails a confirmation link first; nothing arrives until it is clicked
        action = actions.SnsAction(topic)

        status = cw.Metric(
            namespace="AWS/EC2", metric_name="StatusCheckFailed", dimensions_map={"InstanceId": instance_id},
            statistic="Maximum", period=Duration.minutes(1),
        )
        host = cw.Alarm(
            self, "HostStatus", alarm_description="The bot host failed its EC2 status checks (lost network, crashed or impaired).",
            metric=status, threshold=1, comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=3, datapoints_to_alarm=2, treat_missing_data=cw.TreatMissingData.BREACHING,
        )
        beat = cw.Alarm(
            self, "BotHeartbeat", alarm_description="The bot sent no heartbeat for 5 minutes: it is down, hung, or cannot reach AWS.",
            metric=cw.Metric(namespace=NAMESPACE, metric_name=METRIC, statistic="Sum", period=Duration.minutes(1)),
            threshold=1, comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            evaluation_periods=5, datapoints_to_alarm=5, treat_missing_data=cw.TreatMissingData.BREACHING,
        )
        funds = cw.Alarm(
            self, "ExchangeFunds", alarm_description="A Kalshi exchange shard that a live lane trades on holds less cash than it needs, or the account cannot cover them all (src/kalshi_moonshot/fundwatch.py).",
            metric=cw.Metric(namespace=NAMESPACE, metric_name="ExchangeShortfall", statistic="Maximum", period=Duration.minutes(1)),
            threshold=1, comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=3, datapoints_to_alarm=2, treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        for alarm in (host, beat, funds):
            alarm.add_alarm_action(action)
            alarm.add_ok_action(action)  # also say when it recovers

        if role_name:
            role = iam.Role.from_role_name(self, "HostRole", role_name)
            role.attach_inline_policy(iam.Policy(self, "PutHeartbeat", statements=[iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"], resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": NAMESPACE}},
            )]))
