import aws_cdk as cdk
from aws_cdk.assertions import Template
from bot_stack import BotStack
from jobs_stack import JobsStack

ENV = cdk.Environment(account="123456789012", region="us-east-2")


def template(context=None):
    app = cdk.App(context=context or {})
    bot = BotStack(app, "Bot", env=ENV)
    return Template.from_stack(JobsStack(app, "Jobs", env=ENV, vpc=bot.vpc, bot_repo=bot.bot_repo, db_repo=bot.db_repo))


def test_a_job_is_one_arm_task_with_a_database_beside_the_job_and_big_enough_to_leave_the_host_alone():
    t = template()
    t.has_resource_properties("AWS::ECS::TaskDefinition", {"Cpu": "4096", "Memory": "16384", "RuntimePlatform": {"CpuArchitecture": "ARM64", "OperatingSystemFamily": "LINUX"}, "EphemeralStorage": {"SizeInGiB": 60}})
    task = t.find_resources("AWS::ECS::TaskDefinition").popitem()[1]["Properties"]
    boxes = {c["Name"]: c for c in task["ContainerDefinitions"]}
    assert set(boxes) == {"db", "job"}
    assert boxes["job"]["Essential"] is True and boxes["db"]["Essential"] is False  # the task ends when the job does
    assert boxes["job"]["Command"] == ["python", "-m", "kalshi_moonshot.jobrunner"]
    assert boxes["job"]["DependsOn"][0]["Condition"] == "HEALTHY"


def test_the_task_reads_backups_and_writes_only_job_results_and_has_no_inbound():
    t = template({"archive_bucket": "the-bucket"})
    grants = {}
    for policy in t.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if statement.get("Sid") in {"ReadBackupsAndJobInputs", "WriteJobOutputs", "ListTheTwoPrefixes"}:
                grants[statement["Sid"]] = statement
    assert grants["ReadBackupsAndJobInputs"]["Action"] == "s3:GetObject"
    assert set(grants["ReadBackupsAndJobInputs"]["Resource"]) == {"arn:aws:s3:::the-bucket/backups/*", "arn:aws:s3:::the-bucket/jobs/*"}
    assert grants["WriteJobOutputs"]["Action"] == "s3:PutObject" and grants["WriteJobOutputs"]["Resource"] == "arn:aws:s3:::the-bucket/jobs/*"  # never backups/
    assert "s3:DeleteObject" not in str(grants)
    for group in t.find_resources("AWS::EC2::SecurityGroup").values():
        assert group["Properties"].get("SecurityGroupIngress", []) == []


def test_it_costs_nothing_while_idle_and_logs_for_a_week():
    t = template()
    t.has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 7})
    assert not t.find_resources("AWS::ECS::Service")  # a task is started on request, never kept running
    assert {"ClusterName", "TaskDefinition", "SecurityGroup", "Subnets"} <= set(t.find_outputs("*"))
