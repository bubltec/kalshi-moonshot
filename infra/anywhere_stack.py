"""Passwordless AWS from the Mac for the whole bubbletech account: IAM Roles Anywhere (2026-11-28, user: "I don't need to keep doing the SSO thing", "for bubbletech, my business, not just moonshot").

The Mac holds an X.509 client certificate signed by a private CA. AWS trusts that CA (the trust anchor); the CLI's credential_process (`aws_signing_helper`) trades the certificate for 1-hour role credentials on demand: no browser login, no long-lived key.
One trust anchor and one profile for the whole account; one role per project, picked per CLI profile with --role-arn. So far there is one:

  * BubbletechMacMoonshot: only what this repo's scripts call (cloud.sh, cloud_push.sh, cloud_job.sh, publish_site.sh): SSM on the bot host, its parameters, ECR push, the jobs task, the archive bucket.

To add a project later: add another `mac_role(...)` below with that project's own statements, list it in the profile's role_arns, and add an output. The trust anchor, certificate and boundary are shared.

Every role sits under one permission boundary that denies IAM writes, Organizations, billing, SSO, Roles Anywhere itself, CloudTrail, GuardDuty and Config, whatever a policy says. Infrastructure that changes IAM (this stack included) still goes through the SSO admin profile.
The stack is self-contained (it references no other stack) and could move to a shared infra repo; it lives here for now. Deploy once with the SSO profile:

    cdk deploy BubbletechAnywhere --profile jjosefaws -c anywhere_ca_pem=$HOME/.aws/anywhere/ca.pem

Make the CA and client certificate first with scripts/anywhere_setup.sh. Lives in us-east-1 with the account's SSO region; the credentials work in every region.
"""
from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_rolesanywhere as ra
from constructs import Construct

SESSION_SECONDS = 3600


class AnywhereStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = self.node.try_get_context
        ca_path = ctx("anywhere_ca_pem")
        if not ca_path:
            raise ValueError("pass -c anywhere_ca_pem=/path/to/ca.pem (made by scripts/anywhere_setup.sh)")
        ca_pem = Path(str(ca_path)).expanduser().read_text()
        instance_id = str(ctx("bot_instance_id") or "i-0c9a816da43ec969d")
        archive = f"kalshi-moonshot-archive-{self.account}-us-east-1"
        site_bucket = str(ctx("site_bucket") or "")  # the Site stack's BucketName output; empty leaves publish_site.sh on the SSO profile
        acct = self.account

        # Boundary: whatever the role's policy says, these are never allowed.
        boundary = iam.ManagedPolicy(
            self, "Boundary", description="Ceiling for the Mac's Roles Anywhere roles: everything except account-level control.",
            statements=[
                iam.PolicyStatement(effect=iam.Effect.ALLOW, actions=["*"], resources=["*"]),
                iam.PolicyStatement(
                    effect=iam.Effect.DENY, resources=["*"],
                    actions=["iam:Create*", "iam:Put*", "iam:Attach*", "iam:Detach*", "iam:Update*", "iam:Delete*", "iam:Add*", "iam:Remove*", "iam:Set*", "iam:Tag*", "iam:Untag*", "iam:Upload*", "iam:Enable*", "iam:Deactivate*", "iam:Change*", "iam:Reset*", "iam:Resync*", "organizations:*", "account:*", "billing:*", "aws-portal:*", "sso:*", "sso-directory:*",
                             "identitystore:*", "rolesanywhere:*", "cloudtrail:*", "guardduty:*", "config:*", "kms:ScheduleKeyDeletion", "kms:DisableKey"],
                ),
                # iam:PassRole is left out of the deny: the scripts need it for ecs run-task, and the role's own policy limits it to the jobs roles.
            ],
        )

        def mac_role(cid: str, name: str, desc: str) -> iam.Role:
            r = iam.Role(
                self, cid, role_name=name, assumed_by=iam.ServicePrincipal("rolesanywhere.amazonaws.com"),
                max_session_duration=Duration.seconds(SESSION_SECONDS), permissions_boundary=boundary, description=desc,
            )
            assert r.assume_role_policy is not None
            r.assume_role_policy.add_statements(iam.PolicyStatement(  # Roles Anywhere needs these two in the trust policy
                actions=["sts:TagSession", "sts:SetSourceIdentity"], principals=[iam.ServicePrincipal("rolesanywhere.amazonaws.com")],
            ))
            return r

        role = mac_role("MoonshotRole", "BubbletechMacMoonshot", "The Mac, signed in by certificate. Scoped to the kalshi-moonshot scripts.")

        def allow(sid: str, actions: list[str], resources: list[str], **kw) -> None:
            role.add_to_policy(iam.PolicyStatement(sid=sid, actions=actions, resources=resources, **kw))

        # cloud.sh, cloud_push.sh: run commands and open sessions on the bot host, one instance only.
        allow("RunOnBotHost", ["ssm:SendCommand"], [f"arn:aws:ec2:us-east-2:{acct}:instance/{instance_id}", "arn:aws:ssm:us-east-2::document/AWS-RunShellScript"])
        allow("SessionOnBotHost", ["ssm:StartSession"], [f"arn:aws:ec2:us-east-2:{acct}:instance/{instance_id}", "arn:aws:ssm:us-east-2::document/AWS-StartInteractiveCommand", "arn:aws:ssm:us-east-2::document/SSM-SessionManagerRunShell"])
        allow("SessionAndCommandReads", ["ssm:TerminateSession", "ssm:ResumeSession", "ssm:GetCommandInvocation", "ssm:ListCommandInvocations", "ssm:DescribeInstanceInformation", "ssm:DescribeSessions"], ["*"])
        # cloud_push.sh: the bot's dotenv and secret parameters.
        allow("BotParameters", ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "ssm:PutParameter", "ssm:DeleteParameter"], [f"arn:aws:ssm:us-east-2:{acct}:parameter/kalshi-moonshot*"])
        allow("DecryptParameters", ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"], ["*"], conditions={"StringEquals": {"kms:ViaService": "ssm.us-east-2.amazonaws.com"}})
        # cloud_push.sh --build: push the two images.
        allow("EcrLogin", ["ecr:GetAuthorizationToken"], ["*"])
        allow("EcrPush", ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages"],
              [f"arn:aws:ecr:us-east-2:{acct}:repository/kalshi-moonshot-bot", f"arn:aws:ecr:us-east-2:{acct}:repository/kalshi-moonshot-db"])
        # cloud_job.sh: stage inputs, run the Fargate task, read outputs.
        allow("Stacks", ["cloudformation:DescribeStacks"], ["*"])
        allow("JobTasks", ["ecs:RunTask", "ecs:DescribeTasks", "ecs:StopTask", "ecs:ListTasks", "ecs:DescribeTaskDefinition"], ["*"], conditions={"StringEquals": {"aws:RequestedRegion": "us-east-2"}})
        allow("PassJobRoles", ["iam:PassRole"], [f"arn:aws:iam::{acct}:role/KalshiMoonshotJobs-*"], conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}})
        allow("JobLogs", ["logs:GetLogEvents", "logs:FilterLogEvents", "logs:DescribeLogStreams", "logs:DescribeLogGroups"], [f"arn:aws:logs:us-east-2:{acct}:log-group:*"])
        allow("ArchiveRead", ["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"], [f"arn:aws:s3:::{archive}", f"arn:aws:s3:::{archive}/*"])
        allow("JobInputs", ["s3:PutObject"], [f"arn:aws:s3:::{archive}/jobs/*"])
        allow("Athena", ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "athena:StopQueryExecution", "glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:GetPartitions"], ["*"])
        allow("AthenaResults", ["s3:PutObject", "s3:AbortMultipartUpload"], [f"arn:aws:s3:::{archive}/athena-results/*"])
        # publish_site.sh: only when the Site bucket is named at deploy time.
        if site_bucket:
            allow("SiteBucket", ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"], [f"arn:aws:s3:::{site_bucket}", f"arn:aws:s3:::{site_bucket}/*"])
            allow("SiteInvalidate", ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"], [f"arn:aws:cloudfront::{acct}:distribution/*"])
        allow("WhoAmI", ["sts:GetCallerIdentity"], ["*"])
        # cloud watch reads for the watchdog and heartbeat checks
        allow("MetricsRead", ["cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics", "cloudwatch:DescribeAlarms", "cloudwatch:ListMetrics"], ["*"])

        anchor = ra.CfnTrustAnchor(
            self, "Anchor", name="bubbletech-mac", enabled=True,
            source=ra.CfnTrustAnchor.SourceProperty(source_type="CERTIFICATE_BUNDLE", source_data=ra.CfnTrustAnchor.SourceDataProperty(x509_certificate_data=ca_pem)),
        )
        profile = ra.CfnProfile(
            self, "Profile", name="bubbletech-mac", enabled=True, role_arns=[role.role_arn],
            duration_seconds=SESSION_SECONDS,
        )
        CfnOutput(self, "TrustAnchorArn", value=anchor.attr_trust_anchor_arn)
        CfnOutput(self, "ProfileArn", value=profile.attr_profile_arn)
        CfnOutput(self, "MoonshotRoleArn", value=role.role_arn)
