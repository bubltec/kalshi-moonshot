"""The website: kalshi-moonshot.bubbletech.io, a private S3 bucket behind CloudFront, under the shared bubbletech WAF.

Reuses what the sibling repos already own, and creates none of it:
    the bubbletech.io hosted zone     political-sloth's SlothDns stack (imported by id, only a record is added here)
    the shared CLOUDFRONT web ACL     the dotio repo's DotioWaf stack, ARN read from SSM /bubbletech/waf/web-acl-arn
The ACL rate-limits per Host header, so the site needs its own rule there: add { name: 'moonshot', hosts: ['kalshi-moonshot.bubbletech.io'], limit: 1000 }
to WAF_SITES in ../bubbletech.io/infra/cdk/lib/config.ts and deploy DotioWaf before this stack. Until then the shared ACL's default action (allow) applies and the site is unthrottled.

Bucket layout:
    public/index.html, public/assets/*     the built public-site (the default behavior serves only this prefix)
    public/public.json                     counts only; written by the bot
    admin/index.html, admin/assets/*       the built admin-site (the /admin/* behavior: only with CloudFront signed cookies)
    admin/admin.json                       dollars; written by the bot, same behavior
Signing in: /auth/* is a Lambda (infra/admin_auth) that does GitHub OAuth for one allowed numeric GitHub id and sets the signed cookies; CloudFront checks them at the edge
against the trusted key group, so the admin files need no auth code. Keys: scripts/admin_keys.sh makes the pair (private half to SSM, public half to infra/admin_cf_public_key.pem).

Must live in us-east-1: CloudFront's ACM certificate and CLOUDFRONT-scope WAF are us-east-1 only.
"""
from pathlib import Path

from aws_cdk import CfnOutput, Duration, Fn, RemovalPolicy, Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from constructs import Construct

ROOT_DOMAIN = "bubbletech.io"
HOSTED_ZONE_ID = "Z08503632G57OD1I2BJ7V"  # political-sloth's SlothDns ParentZone, the same id dotio and greed pin
WAF_ARN_PARAMETER = "/bubbletech/waf/web-acl-arn"
SITE_DOMAIN = f"kalshi-moonshot.{ROOT_DOMAIN}"

ADMIN_KEY_FILE = Path(__file__).with_name("admin_cf_public_key.pem")
SSM_PREFIX = "/kalshi-moonshot/admin"

# At the edge, before CloudFront checks the cookies: no session cookie goes to the login; /admin/ is the admin page's index.html.
ADMIN_REQUEST_JS = """
function handler(event) {
  var r = event.request;
  if (!r.cookies['CloudFront-Signature']) {
    return { statusCode: 302, statusDescription: 'Found', headers: { location: { value: '/auth/github' }, 'cache-control': { value: 'no-store' } } };
  }
  if (r.uri === '/admin') return { statusCode: 301, statusDescription: 'Moved Permanently', headers: { location: { value: '/admin/' } } };
  if (r.uri.endsWith('/')) r.uri += 'index.html';
  return r;
}
"""

# /admin/* does not match "/admin" itself, so that bare path would fall through to the public site. It needs no cookie: it only points at /admin/, which does.
ADMIN_BARE_JS = """
function handler() {
  return { statusCode: 301, statusDescription: 'Moved Permanently', headers: { location: { value: '/admin/' } } };
}
"""

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
        # Who the bot signs in as: the archive user whose keys are in the bot's .env (environment keys win over the host role), and the host role itself. Both set in cdk.json.
        writers = [str(arn) for arn in (self.node.try_get_context("bot_principal_arns") or [])]
        if writers:
            # Granted here, on the bucket, rather than on the role: the bot stack is in another region and changing it would redeploy the live host.
            bucket.add_to_resource_policy(iam.PolicyStatement(
                sid="BotPublishesThePublicCounts", effect=iam.Effect.ALLOW, principals=[iam.ArnPrincipal(arn) for arn in writers],
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

        if writers:
            bucket.add_to_resource_policy(iam.PolicyStatement(
                sid="BotPublishesTheAdminCounts", effect=iam.Effect.ALLOW, principals=[iam.ArnPrincipal(arn) for arn in writers],
                actions=["s3:PutObject"], resources=[bucket.arn_for_objects("admin/admin.json")],
            ))

        if writers:  # the settings editor: the bot pulls the page's change and says what became of it
            bucket.add_to_resource_policy(iam.PolicyStatement(
                sid="BotReadsTheControlChange", effect=iam.Effect.ALLOW, principals=[iam.ArnPrincipal(arn) for arn in writers],
                actions=["s3:GetObject"], resources=[bucket.arn_for_objects("control/overrides.json"), bucket.arn_for_objects("control/status.json")],
            ))
            bucket.add_to_resource_policy(iam.PolicyStatement(  # S3 answers a read of a missing key with "access denied" unless the reader may list the bucket; the first read, before the page has changed anything, is a missing key
                sid="BotMayTellMissingFromDenied", effect=iam.Effect.ALLOW, principals=[iam.ArnPrincipal(arn) for arn in writers],
                actions=["s3:ListBucket"], resources=[bucket.bucket_arn],
            ))
            bucket.add_to_resource_policy(iam.PolicyStatement(
                sid="BotReportsTheControlStatus", effect=iam.Effect.ALLOW, principals=[iam.ArnPrincipal(arn) for arn in writers],
                actions=["s3:PutObject"], resources=[bucket.arn_for_objects("control/status.json")],
            ))

        admin_behaviors = {}
        key_file = Path(str(self.node.try_get_context("admin_public_key_file") or ADMIN_KEY_FILE))
        if key_file.exists():  # no key yet (scripts/admin_keys.sh): no admin behaviors, only the public site
            public_key = cloudfront.PublicKey(self, "AdminPublicKey", encoded_key=key_file.read_text())
            key_group = cloudfront.KeyGroup(self, "AdminKeyGroup", items=[public_key])
            login = lambda_.Function(
                self, "AdminLogin", runtime=lambda_.Runtime.NODEJS_22_X, handler="index.handler", code=lambda_.Code.from_asset(str(Path(__file__).with_name("admin_auth"))),
                timeout=Duration.seconds(10), memory_size=256,
                environment={
                    "GITHUB_CLIENT_ID": str(self.node.try_get_context("admin_github_client_id") or ""),
                    "ADMIN_GITHUB_IDS": ",".join(str(i) for i in (self.node.try_get_context("admin_github_ids") or [])),
                    "SITE_ORIGIN": f"https://{SITE_DOMAIN}",
                    "CF_KEY_PAIR_ID": public_key.public_key_id,
                    "CLIENT_SECRET_PARAM": f"{SSM_PREFIX}/github-client-secret",
                    "PRIVATE_KEY_PARAM": f"{SSM_PREFIX}/cf-private-key",
                },
            )
            login.add_to_role_policy(iam.PolicyStatement(actions=["ssm:GetParameter"], resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter{SSM_PREFIX}/*"]))
            url = login.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)
            api = lambda_.Function(
                self, "AdminApi", runtime=lambda_.Runtime.NODEJS_22_X, handler="index.handler", code=lambda_.Code.from_asset(str(Path(__file__).with_name("admin_api"))),
                timeout=Duration.seconds(10), memory_size=256,
                environment={
                    "ADMIN_GITHUB_IDS": ",".join(str(i) for i in (self.node.try_get_context("admin_github_ids") or [])),
                    "SITE_ORIGIN": f"https://{SITE_DOMAIN}",
                    "CF_PUBLIC_KEY": key_file.read_text(),
                    "CF_KEY_PAIR_ID": public_key.public_key_id,
                    "BUCKET": bucket.bucket_name,
                    "PRIVATE_KEY_PARAM": f"{SSM_PREFIX}/cf-private-key",
                },
            )
            # Least privilege: the signing key (to check the sign-in token), the two documents it reads, and the two places it writes.
            api.add_to_role_policy(iam.PolicyStatement(actions=["ssm:GetParameter"], resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter{SSM_PREFIX}/cf-private-key"]))
            api.add_to_role_policy(iam.PolicyStatement(actions=["s3:GetObject"], resources=[bucket.arn_for_objects("admin/admin.json"), bucket.arn_for_objects("control/overrides.json")]))
            api.add_to_role_policy(iam.PolicyStatement(actions=["s3:PutObject"], resources=[bucket.arn_for_objects("control/overrides.json"), bucket.arn_for_objects("control/audit/*")]))
            api_url = api.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)  # public like the login's; the Lambda checks the signed cookies itself
            guard = cloudfront.Function(self, "AdminGuard", code=cloudfront.FunctionCode.from_inline(ADMIN_REQUEST_JS), runtime=cloudfront.FunctionRuntime.JS_2_0)
            bare = cloudfront.Function(self, "AdminBare", code=cloudfront.FunctionCode.from_inline(ADMIN_BARE_JS), runtime=cloudfront.FunctionRuntime.JS_2_0)
            admin_behaviors = {
                "/admin": cloudfront.BehaviorOptions(
                    origin=origins.S3BucketOrigin.with_origin_access_control(bucket), viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    response_headers_policy=headers, allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD, cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    function_associations=[cloudfront.FunctionAssociation(function=bare, event_type=cloudfront.FunctionEventType.VIEWER_REQUEST)],
                ),
                "/admin/api/*": cloudfront.BehaviorOptions(  # before /admin/*: the first match wins
                    origin=origins.HttpOrigin(Fn.select(2, Fn.split("/", api_url.url)), protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY, response_headers_policy=headers, allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED, origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    trusted_key_groups=[key_group],
                ),
                "/admin/*": cloudfront.BehaviorOptions(
                    origin=origins.S3BucketOrigin.with_origin_access_control(bucket), viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    response_headers_policy=headers, allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD, cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    trusted_key_groups=[key_group],
                    function_associations=[cloudfront.FunctionAssociation(function=guard, event_type=cloudfront.FunctionEventType.VIEWER_REQUEST)],
                ),
                "/auth/*": cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(Fn.select(2, Fn.split("/", url.url)), protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY, response_headers_policy=headers, allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED, origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                ),
            }

        distribution = cloudfront.Distribution(
            self, "Distribution",
            domain_names=[SITE_DOMAIN], certificate=certificate, default_root_object="index.html",
            web_acl_id=ssm.StringParameter.value_for_string_parameter(self, WAF_ARN_PARAMETER),  # owned by DotioWaf: one ACL for every bubbletech site
            default_behavior=cloudfront.BehaviorOptions(cache_policy=short, **behavior),
            additional_behaviors={"/assets/*": cloudfront.BehaviorOptions(cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED, **behavior), **admin_behaviors},
            error_responses=[cloudfront.ErrorResponse(http_status=403, response_http_status=404, response_page_path="/index.html", ttl=Duration.seconds(30))],
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        )
        target = route53.RecordTarget.from_alias(targets.CloudFrontTarget(distribution))
        route53.ARecord(self, "AliasA", zone=zone, record_name=SITE_DOMAIN, target=target)
        route53.AaaaRecord(self, "AliasAAAA", zone=zone, record_name=SITE_DOMAIN, target=target)

        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        CfnOutput(self, "SiteUrl", value=f"https://{SITE_DOMAIN}")
