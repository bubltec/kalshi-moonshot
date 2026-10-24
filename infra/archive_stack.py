"""The archive stack: one private bucket for old tape data and database backups, an IAM user the archiver runs as, and Athena over the Parquet files.

Layout inside the bucket (the archiver in ../src/kalshi_moonshot writes exactly this):
    tape/<table>/day=YYYY-MM-DD/part-0.parquet     one file per table per day (quotes, flow_1s, index_ticks, feed_ticks, fut_1s)
    backups/<name>-YYYYMMDD.dump                    the nightly pg_dump files
    athena-results/                                 Athena query output, deleted after a week

The archiver user can write and read but never delete; removing data from the bucket is a human decision made with your SSO login.
"""
from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_athena as athena
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

# Columns of the tape tables as they are written to Parquet (see ../src/kalshi_moonshot/tape.py). `day` is the partition, not a column in the file.
TAPE_TABLES: dict[str, list[tuple[str, str]]] = {
    "quotes": [("ts", "double"), ("ticker", "string"), ("bid", "double"), ("ask", "double"), ("last", "double"), ("sts", "bigint"), ("bid_size", "double"), ("ask_size", "double")],
    "flow_1s": [("ts", "bigint"), ("ticker", "string"), ("yes_vol", "double"), ("no_vol", "double"), ("n", "int"), ("vwap", "double")],
    "index_ticks": [("ts", "double"), ("index_id", "string"), ("value", "double"), ("avg60", "double"), ("quarter_avg", "double"), ("src_ms", "bigint")],
    "feed_ticks": [("ts", "double"), ("symbol", "string"), ("px", "double")],
    "fut_1s": [("ts", "bigint"), ("symbol", "string"), ("px", "double"), ("buy_usd", "double"), ("sell_usd", "double"), ("buy_n", "int"), ("sell_n", "int")],
}
PARQUET_IN = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
PARQUET_OUT = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"
PARQUET_SERDE = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
FIRST_DAY = "2026-10-01"  # Athena looks for day partitions from here to today; older days simply do not exist


class ArchiveStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = self.node.try_get_context
        backup_days = int(ctx("backup_expire_days") or 400)
        scan_gb = int(ctx("athena_scan_limit_gb") or 10)

        bucket = s3.Bucket(
            self, "Archive",
            bucket_name=f"kalshi-moonshot-archive-{self.account}-{self.region}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,  # an overwritten or deleted file can be recovered for 30 days
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.RETAIN,  # deleting the stack never deletes the data
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="tape-intelligent-tiering", prefix="tape/",
                    transitions=[s3.Transition(storage_class=s3.StorageClass.INTELLIGENT_TIERING, transition_after=Duration.days(0))],
                ),
                s3.LifecycleRule(
                    id="backups", prefix="backups/",
                    transitions=[s3.Transition(storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL, transition_after=Duration.days(30))],
                    expiration=Duration.days(backup_days),
                ),
                s3.LifecycleRule(id="athena-results", prefix="athena-results/", expiration=Duration.days(7)),
                s3.LifecycleRule(
                    id="housekeeping",
                    noncurrent_version_expiration=Duration.days(30),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                ),
            ],
        )

        # The identity the archiver runs as. Static access keys are created by hand (aws iam create-access-key) and kept in .env,
        # so no secret ever passes through CloudFormation. It can add and read objects, never delete them.
        writer = iam.User(self, "ArchiveWriter", user_name="kalshi-moonshot-archive-writer")
        writer.add_to_policy(iam.PolicyStatement(
            sid="ReadWriteObjects",
            actions=["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
            resources=[bucket.arn_for_objects("tape/*"), bucket.arn_for_objects("backups/*")],
        ))
        writer.add_to_policy(iam.PolicyStatement(
            sid="ListTheTwoPrefixes",
            actions=["s3:ListBucket", "s3:GetBucketLocation"],
            resources=[bucket.bucket_arn],
            conditions={"StringLike": {"s3:prefix": ["tape/*", "backups/*", "tape", "backups", ""]}},
        ))

        # Athena over the Parquet files: SQL on years of ticks without restoring anything. Each query is cut off at scan_gb.
        db_name = "kalshi_moonshot_archive"
        glue.CfnDatabase(self, "Database", catalog_id=self.account, database_input=glue.CfnDatabase.DatabaseInputProperty(name=db_name))
        athena.CfnWorkGroup(
            self, "WorkGroup", name="kalshi-moonshot-archive",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
                bytes_scanned_cutoff_per_query=scan_gb * 1024**3,
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{bucket.bucket_name}/athena-results/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(encryption_option="SSE_S3"),
                ),
            ),
        )
        for name, columns in TAPE_TABLES.items():
            glue.CfnTable(
                self, f"Table{name}", catalog_id=self.account, database_name=db_name,
                table_input=glue.CfnTable.TableInputProperty(
                    name=name, table_type="EXTERNAL_TABLE",
                    parameters={
                        "classification": "parquet",
                        "projection.enabled": "true",  # days are computed, so nothing has to register each new partition
                        "projection.day.type": "date",
                        "projection.day.format": "yyyy-MM-dd",
                        "projection.day.range": f"{FIRST_DAY},NOW",
                        "projection.day.interval": "1",
                        "projection.day.interval.unit": "DAYS",
                        "storage.location.template": f"s3://{bucket.bucket_name}/tape/{name}/day=" + "${day}/",
                    },
                    partition_keys=[glue.CfnTable.ColumnProperty(name="day", type="string")],
                    storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                        columns=[glue.CfnTable.ColumnProperty(name=c, type=t) for c, t in columns],
                        location=f"s3://{bucket.bucket_name}/tape/{name}/",
                        input_format=PARQUET_IN, output_format=PARQUET_OUT,
                        serde_info=glue.CfnTable.SerdeInfoProperty(serialization_library=PARQUET_SERDE),
                    ),
                ),
            ).node.add_dependency(self.node.find_child("Database"))

        # Optional: an email when the month's AWS spend for the account passes 80% of a limit. Off unless -c alert_email=... is given.
        if email := ctx("alert_email"):
            budgets.CfnBudget(
                self, "Budget",
                budget=budgets.CfnBudget.BudgetDataProperty(
                    budget_type="COST", time_unit="MONTHLY",
                    budget_limit=budgets.CfnBudget.SpendProperty(amount=float(ctx("budget_usd") or 10), unit="USD"),
                ),
                notifications_with_subscribers=[budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(notification_type="ACTUAL", comparison_operator="GREATER_THAN", threshold=80, threshold_type="PERCENTAGE"),
                    subscribers=[budgets.CfnBudget.SubscriberProperty(subscription_type="EMAIL", address=email)],
                )],
            )

        CfnOutput(self, "BucketName", value=bucket.bucket_name, description="ARCHIVE_BUCKET in .env")
        CfnOutput(self, "WriterUser", value=writer.user_name, description="create its access key by hand; see README")
        CfnOutput(self, "AthenaWorkGroup", value="kalshi-moonshot-archive")
        CfnOutput(self, "AthenaDatabase", value=db_name)
