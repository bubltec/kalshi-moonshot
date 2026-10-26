#!/usr/bin/env python3
"""The infrastructure for the data archive, as code. Run from this folder:

    uv venv .venv && uv pip install -p .venv -r requirements.txt     # once
    cdk synth                       # build the CloudFormation template; needs no AWS login
    cdk diff --profile <sso-profile>
    cdk deploy --profile <sso-profile>

Account and region come from cdk.json (or -c account=... -c region=...). Nothing here runs the bot; see ../README.md, "Archive".
"""
import os

import aws_cdk as cdk
from archive_stack import ArchiveStack

app = cdk.App()
ArchiveStack(
    app,
    "KalshiMoonshotArchive",
    env=cdk.Environment(
        account=app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION"),
    ),
    description="Cold storage for the bot's tape and backups: an S3 bucket, a write-only-ish user for the archiver, and Athena over the Parquet files.",
)
cdk.Tags.of(app).add("project", "kalshi-moonshot")
app.synth()
