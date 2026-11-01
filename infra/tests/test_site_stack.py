import json

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template
from site_stack import SITE_DOMAIN, SiteStack


def template():
    app = cdk.App()
    return Template.from_stack(SiteStack(app, "Site", env=cdk.Environment(account="123456789012", region="us-east-1")))


def test_the_site_sits_on_the_shared_waf_and_the_bubbletech_zone():
    t = template()
    assert SITE_DOMAIN == "kalshi-moonshot.bubbletech.io"
    dist = next(iter(t.find_resources("AWS::CloudFront::Distribution").values()))["Properties"]["DistributionConfig"]
    assert dist["Aliases"] == [SITE_DOMAIN]
    assert "WebACLId" in dist  # read from /bubbletech/waf/web-acl-arn, never created here
    t.resource_count_is("AWS::WAFv2::WebACL", 0)
    t.resource_count_is("AWS::Route53::HostedZone", 0)  # the zone is SlothDns's
    t.has_resource_properties("AWS::Route53::RecordSet", {"Name": f"{SITE_DOMAIN}.", "Type": "A"})


def test_the_bucket_is_private_and_only_the_public_prefix_is_served():
    t = template()
    t.has_resource_properties("AWS::S3::Bucket", {"PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True}})
    t.has_resource_properties("AWS::CloudFront::Distribution", {"DistributionConfig": Match.object_like({"Origins": Match.array_with([Match.object_like({"OriginPath": "/public"})])})})
    assert next(iter(t.find_resources("AWS::S3::Bucket").values()))["DeletionPolicy"] == "Retain"
    t.has_resource_properties("AWS::CloudFront::ResponseHeadersPolicy", {"ResponseHeadersPolicyConfig": Match.object_like({"CustomHeadersConfig": {"Items": [Match.object_like({"Header": "X-Robots-Tag"})]}})})


def test_the_bot_may_write_only_the_public_counts_object():
    user, role = "arn:aws:iam::123456789012:user/archive-writer", "arn:aws:iam::123456789012:role/Bot-HostRole"
    app = cdk.App(context={"bot_principal_arns": [user, role]})
    t = Template.from_stack(SiteStack(app, "Site", env=cdk.Environment(account="123456789012", region="us-east-1")))
    t.has_resource_properties("AWS::S3::BucketPolicy", {"PolicyDocument": {"Statement": Match.array_with([
        Match.object_like({"Sid": "BotPublishesThePublicCounts", "Action": "s3:PutObject", "Principal": {"AWS": [user, role]}})])}})


def test_the_admin_area_needs_signed_cookies_and_the_login_lambda_is_wired(tmp_path):
    import subprocess

    key = tmp_path / "pub.pem"
    private = subprocess.run(["openssl", "genrsa", "2048"], capture_output=True, check=True).stdout
    key.write_bytes(subprocess.run(["openssl", "rsa", "-pubout"], input=private, capture_output=True, check=True).stdout)
    app = cdk.App(context={"admin_public_key_file": str(key), "admin_github_ids": ["194750"], "admin_github_client_id": "cid", "bot_principal_arns": ["arn:aws:iam::123456789012:user/w"]})
    t = Template.from_stack(SiteStack(app, "Site", env=cdk.Environment(account="123456789012", region="us-east-1")))
    dist = next(iter(t.find_resources("AWS::CloudFront::Distribution").values()))["Properties"]["DistributionConfig"]
    by_path = {b["PathPattern"]: b for b in dist["CacheBehaviors"]}
    assert "TrustedKeyGroups" in by_path["/admin/*"] and "TrustedKeyGroups" not in dist["DefaultCacheBehavior"]
    assert by_path["/admin/*"]["ViewerProtocolPolicy"] == "redirect-to-https" and "/auth/*" in by_path
    t.has_resource_properties("AWS::Lambda::Function", {"Runtime": "nodejs22.x", "Environment": {"Variables": Match.object_like({"ADMIN_GITHUB_IDS": "194750", "GITHUB_CLIENT_ID": "cid"})}})
    t.has_resource_properties("AWS::S3::BucketPolicy", {"PolicyDocument": {"Statement": Match.array_with([
        Match.object_like({"Sid": "BotPublishesTheAdminCounts", "Resource": Match.any_value()})])}})
    # the settings editor: its route needs the cookies too and comes before /admin/* (the first match wins); the bot reads one change and reports one status
    paths = [b["PathPattern"] for b in dist["CacheBehaviors"]]
    assert paths.index("/admin/api/*") < paths.index("/admin/*") and "TrustedKeyGroups" in by_path["/admin/api/*"]
    assert by_path["/admin/api/*"]["ViewerProtocolPolicy"] == "https-only" and "POST" in by_path["/admin/api/*"]["AllowedMethods"]
    assert "POST" not in by_path["/admin/*"]["AllowedMethods"] and "POST" not in by_path["/auth/*"]["AllowedMethods"]
    t.has_resource_properties("AWS::S3::BucketPolicy", {"PolicyDocument": {"Statement": Match.array_with([
        Match.object_like({"Sid": "BotReadsTheControlChange", "Action": "s3:GetObject"}),
        Match.object_like({"Sid": "BotMayTellMissingFromDenied", "Action": "s3:ListBucket"}),
        Match.object_like({"Sid": "BotReportsTheControlStatus", "Action": "s3:PutObject"})])}})
    # least privilege for the API Lambda: only the signing key, and these S3 keys
    api = next(r for r in t.find_resources("AWS::Lambda::Function").values() if "CF_PUBLIC_KEY" in r["Properties"]["Environment"]["Variables"])
    assert "BEGIN PUBLIC KEY" in api["Properties"]["Environment"]["Variables"]["CF_PUBLIC_KEY"]
    policy = json.dumps([p["Properties"]["PolicyDocument"] for k, p in t.find_resources("AWS::IAM::Policy").items() if "AdminApi" in k])
    assert "cf-private-key" in policy and "github-client-secret" not in policy and '"s3:DeleteObject"' not in policy and "control/audit/*" in policy


def test_without_a_key_there_is_no_admin_area(tmp_path):
    app = cdk.App(context={"admin_public_key_file": str(tmp_path / "missing.pem")})  # the repo's own key file exists now
    t = Template.from_stack(SiteStack(app, "Site", env=cdk.Environment(account="123456789012", region="us-east-1")))
    dist = next(iter(t.find_resources("AWS::CloudFront::Distribution").values()))["Properties"]["DistributionConfig"]
    assert [b["PathPattern"] for b in dist["CacheBehaviors"]] == ["/assets/*"]
    t.resource_count_is("AWS::Lambda::Function", 0)
