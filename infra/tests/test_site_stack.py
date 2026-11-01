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


def test_the_bot_role_may_write_only_the_public_counts_object():
    arn = "arn:aws:iam::123456789012:role/Bot-HostRole"
    app = cdk.App(context={"bot_role_arn": arn})
    t = Template.from_stack(SiteStack(app, "Site", env=cdk.Environment(account="123456789012", region="us-east-1")))
    t.has_resource_properties("AWS::S3::BucketPolicy", {"PolicyDocument": {"Statement": Match.array_with([
        Match.object_like({"Sid": "BotPublishesThePublicCounts", "Action": "s3:PutObject", "Principal": {"AWS": arn}})])}})
