#!/usr/bin/env python3
"""The infrastructure, as code. Run from this folder:

    uv venv .venv && uv pip install -p .venv -r requirements.txt     # once
    cdk synth                       # build the CloudFormation template; needs no AWS login
    cdk diff --profile <sso-profile>
    cdk deploy --profile <sso-profile> KalshiMoonshotBot

The archive stays in the region from cdk.json. The bot runs in us-east-2 (Ohio), close to Kalshi.
See ../README.md, "Archive" and "Running in Ohio".
"""
import os

import aws_cdk as cdk
from archive_stack import ArchiveStack
from bot_stack import BotStack
from jobs_stack import JobsStack
from site_stack import SiteStack
from watchdog_stack import WatchdogStack

app = cdk.App()
account = app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT")
ArchiveStack(
    app,
    "KalshiMoonshotArchive",
    env=cdk.Environment(
        account=account,
        region=app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION"),
    ),
    description="Cold storage for the bot's tape and backups: an S3 bucket, a write-only-ish user for the archiver, and Athena over the Parquet files.",
)
BotStack(
    app,
    "KalshiMoonshotBot",
    env=cdk.Environment(account=account, region=app.node.try_get_context("bot_region") or "us-east-2"),
    description="One host in Ohio that runs the bot and its database. The model stays on the Mac.",
)
JobsStack(
    app,
    "KalshiMoonshotJobs",
    env=cdk.Environment(account=account, region=app.node.try_get_context("bot_region") or "us-east-2"),
    description="Throwaway Fargate tasks for heavy jobs (refits, walk-forwards): restore the newest backups, run one script, write the result to the archive bucket. Start one with scripts/cloud_job.sh.",
)
SiteStack(
    app,
    "KalshiMoonshotSite",
    env=cdk.Environment(account=account, region="us-east-1"),  # CloudFront's certificate and the shared WAF live in us-east-1
    description="kalshi-moonshot.bubbletech.io: a private bucket behind CloudFront and the shared bubbletech WAF.",
)
WatchdogStack(
    app,
    "KalshiMoonshotWatchdog",
    env=cdk.Environment(account=account, region=app.node.try_get_context("bot_region") or "us-east-2"),
    description="Emails when the live bot stops: EC2 status-check alarm plus a heartbeat alarm. Self-contained; never touches the bot stack. Needs -c alert_email=... -c bot_role_name=...",
)
if app.node.try_get_context("anywhere_ca_pem"):  # only when asked for, so a plain `cdk synth` of the other stacks needs no certificate
    from anywhere_stack import AnywhereStack

    AnywhereStack(
        app,
        "BubbletechAnywhere",
        env=cdk.Environment(account=account, region="us-east-1"),
        description="Passwordless AWS from the Mac for all of bubbletech: IAM Roles Anywhere trust anchor, profile and a moonshot-scoped role under a permission boundary; more projects add their own role later. Needs -c anywhere_ca_pem=... (scripts/anywhere_setup.sh keys).",
    )
cdk.Tags.of(app).add("project", "kalshi-moonshot")
app.synth()
