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
cdk.Tags.of(app).add("project", "kalshi-moonshot")
app.synth()
