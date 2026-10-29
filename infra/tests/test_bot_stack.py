import aws_cdk as cdk
from aws_cdk.assertions import Match, Template
from bot_stack import BotStack


def template():
    app = cdk.App()
    return Template.from_stack(BotStack(app, "Bot", env=cdk.Environment(account="123456789012", region="us-east-2")))


def test_the_host_is_a_small_arm_machine_with_no_inbound_and_a_kept_data_disk():
    t = template()
    t.has_resource_properties("AWS::EC2::Instance", {"InstanceType": "t4g.medium"})
    t.has_resource_properties("AWS::EC2::Volume", {"Size": 100, "Encrypted": True})
    host_az = t.find_resources("AWS::EC2::Instance").popitem()[1]["Properties"]["AvailabilityZone"]
    disk = t.find_resources("AWS::EC2::Volume").popitem()[1]
    assert disk["Properties"]["AvailabilityZone"] == host_az
    assert disk["DeletionPolicy"] == "Retain"
    for group in t.find_resources("AWS::EC2::SecurityGroup").values():
        assert group["Properties"].get("SecurityGroupIngress", []) == []


def test_the_host_can_pull_images_and_read_its_env_and_nothing_is_public():
    t = template()
    actions = set()
    for policy in t.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement["Action"]
            actions.update(action if isinstance(action, list) else [action])
    assert {"ssm:GetParameter", "ecr:BatchGetImage", "ecr:GetAuthorizationToken"} <= actions
    repos = t.find_resources("AWS::ECR::Repository")
    assert {r["Properties"]["RepositoryName"] for r in repos.values()} == {"kalshi-moonshot-bot", "kalshi-moonshot-db"}
    for repo in repos.values():
        assert repo["DeletionPolicy"] == "Retain"


def test_instance_type_and_disk_size_come_from_context():
    app = cdk.App(context={"bot_instance": "t4g.small", "bot_volume_gb": 40})
    t = Template.from_stack(BotStack(app, "Bot", env=cdk.Environment(account="123456789012", region="us-east-2")))
    t.has_resource_properties("AWS::EC2::Instance", {"InstanceType": "t4g.small"})
    t.has_resource_properties("AWS::EC2::Volume", Match.object_like({"Size": 40}))
