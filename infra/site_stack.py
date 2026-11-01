"""The website: kalshi-moonshot.bubbletech.io, a private S3 bucket behind CloudFront, under the shared bubbletech WAF.

Reuses what the sibling repos already own, and creates none of it:
    the bubbletech.io hosted zone     political-sloth's SlothDns stack (imported by id, only a record is added here)
    the shared CLOUDFRONT web ACL     the dotio repo's DotioWaf stack, ARN read from SSM /bubbletech/waf/web-acl-arn
The ACL rate-limits per Host header, so the site needs its own rule there: add { name: 'moonshot', hosts: ['kalshi-moonshot.bubbletech.io'], limit: 1000 }
to WAF_SITES in ../bubbletech.io/infra/cdk/lib/config.ts and deploy DotioWaf before this stack. Until then the shared ACL's default action (allow) applies and the site is unthrottled.

Bucket layout (CloudFront serves only public/, so nothing else in the bucket is reachable from the web):
    public/index.html, public/assets/*     the built public-site
    public/public.json                     counts only; written by the exporter
    admin/                                 reserved for admin.json (never behind this distribution's public behavior)

Must live in us-east-1: CloudFront's ACM certificate and CLOUDFRONT-scope WAF are us-east-1 only.
"""
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_iam as iam
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from constructs import Construct

ROOT_DOMAIN = "bubbletech.io"
HOSTED_ZONE_ID = "Z08503632G57OD1I2BJ7V"  # political-sloth's SlothDns ParentZone, the same id dotio and greed pin
WAF_ARN_PARAMETER = "/bubbletech/waf/web-acl-arn"
SITE_DOMAIN = f"kalshi-moonshot.{ROOT_DOMAIN}"

CSP = "default-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


class SiteStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        zone = route53.PublicHostedZone.from_hosted_zone_attributes(self, "Zone", hosted_zone_id=HOSTED_ZONE_ID, zone_name=ROOT_DOMAIN)

        bucket = s3.Bucket(
            self, "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True, encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
        )
        bot_role = str(self.node.try_get_context("bot_role_arn") or "")  # the bot host's role (KalshiMoonshotBot HostRole), set in cdk.json
        if bot_role:
            # Granted here, on the bucket, rather than on the role: the bot stack is in another region and changing it would redeploy the live host.
            bucket.add_to_resource_policy(iam.PolicyStatement(
                sid="BotPublishesThePublicCounts", effect=iam.Effect.ALLOW, principals=[iam.ArnPrincipal(bot_role)],
                actions=["s3:PutObject"], resources=[bucket.arn_for_objects("public/public.json")],  # that one object: no other key, no read, no delete
            ))
        certificate = acm.Certificate(self, "Certificate", domain_name=SITE_DOMAIN, validation=acm.CertificateValidation.from_dns(zone))

        headers = cloudfront.ResponseHeadersPolicy(
            self, "Headers",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(frame_option=cloudfront.HeadersFrameOption.DENY, override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER, override=True),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(access_control_max_age=Duration.days(365), include_subdomains=False, override=True),
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(content_security_policy=CSP, override=True),
            ),
            custom_headers_behavior=cloudfront.ResponseCustomHeadersBehavior(
                custom_headers=[cloudfront.ResponseCustomHeader(header="X-Robots-Tag", value="noindex, nofollow", override=True)]
            ),
        )
        # The counts change at most hourly, so a short edge cache keeps the page fresh and a flood of reads off S3.
        short = cloudfront.CachePolicy(self, "ShortCache", default_ttl=Duration.minutes(1), min_ttl=Duration.seconds(0), max_ttl=Duration.minutes(5), enable_accept_encoding_gzip=True, enable_accept_encoding_brotli=True)
        origin = origins.S3BucketOrigin.with_origin_access_control(bucket, origin_path="/public")
        behavior = dict(origin=origin, viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS, response_headers_policy=headers, allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD)

        distribution = cloudfront.Distribution(
            self, "Distribution",
            domain_names=[SITE_DOMAIN], certificate=certificate, default_root_object="index.html",
            web_acl_id=ssm.StringParameter.value_for_string_parameter(self, WAF_ARN_PARAMETER),  # owned by DotioWaf: one ACL for every bubbletech site
            default_behavior=cloudfront.BehaviorOptions(cache_policy=short, **behavior),
            additional_behaviors={"/assets/*": cloudfront.BehaviorOptions(cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED, **behavior)},
            error_responses=[cloudfront.ErrorResponse(http_status=403, response_http_status=404, response_page_path="/index.html", ttl=Duration.seconds(30))],
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        )
        target = route53.RecordTarget.from_alias(targets.CloudFrontTarget(distribution))
        route53.ARecord(self, "AliasA", zone=zone, record_name=SITE_DOMAIN, target=target)
        route53.AaaaRecord(self, "AliasAAAA", zone=zone, record_name=SITE_DOMAIN, target=target)

        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        CfnOutput(self, "SiteUrl", value=f"https://{SITE_DOMAIN}")
