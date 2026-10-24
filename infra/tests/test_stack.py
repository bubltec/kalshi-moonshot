import aws_cdk as cdk
from archive_stack import ArchiveStack
from aws_cdk.assertions import Match, Template


def template(**context):
    app = cdk.App(context=context)
    return Template.from_stack(ArchiveStack(app, "T", env=cdk.Environment(account="123456789012", region="us-east-1")))


def test_the_bucket_is_private_encrypted_versioned_and_never_deleted_with_the_stack():
    t = template()
    t.has_resource("AWS::S3::Bucket", {"DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain"})
    t.has_resource_properties("AWS::S3::Bucket", {
        "BucketName": "kalshi-moonshot-archive-123456789012-us-east-1",
        "PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
        "VersioningConfiguration": {"Status": "Enabled"},
        "BucketEncryption": Match.any_value(),
    })
    t.has_resource_properties("AWS::S3::BucketPolicy", {"PolicyDocument": {"Statement": Match.array_with([Match.object_like({"Effect": "Deny", "Condition": {"Bool": {"aws:SecureTransport": "false"}}})])}})


def test_lifecycle_tiers_the_tape_expires_backups_and_cleans_up():
    rules = {r["Id"]: r for r in template().find_resources("AWS::S3::Bucket").popitem()[1]["Properties"]["LifecycleConfiguration"]["Rules"]}
    assert rules["tape-intelligent-tiering"]["Transitions"][0]["StorageClass"] == "INTELLIGENT_TIERING"
    assert rules["backups"]["ExpirationInDays"] == 400 and rules["backups"]["Transitions"][0]["StorageClass"] == "GLACIER_IR"
    assert rules["athena-results"]["ExpirationInDays"] == 7
    assert rules["housekeeping"]["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 7


def test_the_archiver_can_write_and_read_but_has_no_delete_permission_anywhere():
    t = template()
    statements = [s for p in t.find_resources("AWS::IAM::Policy").values() for s in p["Properties"]["PolicyDocument"]["Statement"]]
    actions = {a for s in statements for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])}
    assert {"s3:PutObject", "s3:GetObject", "s3:ListBucket"} <= actions
    assert not [a for a in actions if "Delete" in a or a == "s3:*" or a == "*"]


def test_athena_has_a_scan_limit_and_tables_with_computed_day_partitions():
    t = template(athena_scan_limit_gb=5)
    t.has_resource_properties("AWS::Athena::WorkGroup", {"WorkGroupConfiguration": Match.object_like({"BytesScannedCutoffPerQuery": 5 * 1024**3, "EnforceWorkGroupConfiguration": True})})
    tables = t.find_resources("AWS::Glue::Table")
    assert len(tables) == 5
    for res in tables.values():
        params = res["Properties"]["TableInput"]["Parameters"]
        assert params["projection.enabled"] == "true" and params["projection.day.range"].endswith(",NOW")


def test_the_budget_alert_exists_only_when_an_email_is_given():
    assert not template().find_resources("AWS::Budgets::Budget")
    assert len(template(alert_email="me@example.com", budget_usd=7).find_resources("AWS::Budgets::Budget")) == 1
