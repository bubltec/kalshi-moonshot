import aws_cdk as cdk
from aws_cdk.assertions import Match, Template
from watchdog_stack import WatchdogStack

ENV = cdk.Environment(account="123456789012", region="us-east-2")


def template(context=None):
    return Template.from_stack(WatchdogStack(cdk.App(context=context or {}), "W", env=ENV))


def test_two_alarms_treat_missing_data_as_bad_and_mail_the_topic():
    t = template({"alert_email": "a@example.com", "bot_role_name": "HostRole"})
    t.has_resource_properties("AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "a@example.com"})
    alarms = t.find_resources("AWS::CloudWatch::Alarm")
    assert len(alarms) == 2
    for a in alarms.values():
        assert a["Properties"]["TreatMissingData"] == "breaching"
        assert a["Properties"]["AlarmActions"] and a["Properties"]["OKActions"]
    t.has_resource_properties("AWS::CloudWatch::Alarm", Match.object_like({"MetricName": "Heartbeat", "EvaluationPeriods": 5, "DatapointsToAlarm": 5, "Period": 60}))
    t.has_resource_properties("AWS::CloudWatch::Alarm", Match.object_like({"MetricName": "StatusCheckFailed", "Namespace": "AWS/EC2"}))


def test_the_host_role_may_only_put_the_heartbeat_namespace_and_the_stack_has_no_bot_stack_resources():
    t = template({"bot_role_name": "HostRole"})
    t.has_resource_properties("AWS::IAM::Policy", {
        "Roles": ["HostRole"],
        "PolicyDocument": {"Statement": [Match.object_like({"Action": "cloudwatch:PutMetricData", "Condition": {"StringEquals": {"cloudwatch:namespace": "KalshiMoonshot/Bot"}}})]},
    })
    assert not t.find_resources("AWS::EC2::Instance")


def test_no_role_name_means_no_policy():
    assert not template().find_resources("AWS::IAM::Policy")
