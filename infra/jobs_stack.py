"""Throwaway compute for the heavy jobs: a model refit, a walk-forward study, a long review (2026-11-09, user: "run it from a temporary instance or task on ECS").

The bot host is a 2-CPU machine with spent burst credits. A refit that also trains on the commodity series pinned it at 98% on 11-09 and cut off its remote-command agent. These jobs run somewhere else instead:
one Fargate task, started only when asked, that restores the newest nightly backups from the archive bucket (moonshot-*.dump, tape-*.dump, written by the bot's archiver) into its own Postgres, runs one script, writes the output back to the bucket under jobs/<id>/, and ends. Nothing touches the live host or its database.

Two containers share the task's network: the database image (Postgres 18 with pgvector) and the bot image running kalshi_moonshot.jobrunner. The job is the essential container; when it ends, the task ends and the database goes with it. The task has no inbound rules and holds no keys: its role reads backups/ and jobs/ and writes jobs/ in the archive bucket, nothing else.
Start one with scripts/cloud_job.sh. The data is as old as the last nightly backup (up to about a day): right for refits and walk-forwards, not for a review of the last few hours.
"""
from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

TASK_CPU = 4096  # 4 vCPUs: the bot host has 2 and is the thing being protected
TASK_MEMORY_MIB = 16384
EPHEMERAL_GIB = 60  # the restored tape and the working files; gone when the task ends


class JobsStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, vpc: ec2.IVpc, bot_repo: ecr.IRepository, db_repo: ecr.IRepository, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = self.node.try_get_context
        bucket = str(ctx("archive_bucket") or "kalshi-moonshot-archive-placeholder")  # the archive stack's bucket name; pass -c archive_bucket=NAME to deploy
        archive_region = str(ctx("archive_region") or "us-east-1")

        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, enable_fargate_capacity_providers=True)
        sg = ec2.SecurityGroup(self, "JobSg", vpc=vpc, description="Job tasks. No inbound; outbound only.", allow_all_outbound=True)
        logs_group = logs.LogGroup(self, "Logs", retention=logs.RetentionDays.ONE_WEEK, removal_policy=RemovalPolicy.DESTROY)

        task_role = iam.Role(self, "JobRole", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        task_role.add_to_policy(iam.PolicyStatement(
            sid="ReadBackupsAndJobInputs", actions=["s3:GetObject"],
            resources=[f"arn:aws:s3:::{bucket}/backups/*", f"arn:aws:s3:::{bucket}/jobs/*"],
        ))
        task_role.add_to_policy(iam.PolicyStatement(
            sid="WriteJobOutputs", actions=["s3:PutObject"], resources=[f"arn:aws:s3:::{bucket}/jobs/*"],
        ))
        task_role.add_to_policy(iam.PolicyStatement(
            sid="ListTheTwoPrefixes", actions=["s3:ListBucket"], resources=[f"arn:aws:s3:::{bucket}"],
            conditions={"StringLike": {"s3:prefix": ["backups/*", "jobs/*"]}},
        ))

        task = ecs.FargateTaskDefinition(
            self, "Task", cpu=TASK_CPU, memory_limit_mib=TASK_MEMORY_MIB, ephemeral_storage_gib=EPHEMERAL_GIB, task_role=task_role,
            runtime_platform=ecs.RuntimePlatform(cpu_architecture=ecs.CpuArchitecture.ARM64, operating_system_family=ecs.OperatingSystemFamily.LINUX),
        )
        db = task.add_container(
            "db", image=ecs.ContainerImage.from_ecr_repository(db_repo, "latest"), essential=False,
            environment={"POSTGRES_USER": "moonshot", "POSTGRES_PASSWORD": "moonshot", "POSTGRES_DB": "moonshot"},  # reachable only inside this task: no inbound rule exists
            command=["postgres", "-c", "shared_buffers=1GB", "-c", "synchronous_commit=off", "-c", "fsync=off", "-c", "max_connections=40"],  # a throwaway database: speed over durability
            health_check=ecs.HealthCheck(command=["CMD-SHELL", "pg_isready -U moonshot -d moonshot"], interval=Duration.seconds(5), timeout=Duration.seconds(5), retries=20),
            logging=ecs.LogDriver.aws_logs(stream_prefix="db", log_group=logs_group),
        )
        job = task.add_container(
            "job", image=ecs.ContainerImage.from_ecr_repository(bot_repo, "latest"), essential=True,
            command=["python", "-m", "kalshi_moonshot.jobrunner"],
            environment={
                "DATABASE_URL": "postgresql://moonshot:moonshot@localhost:5432/moonshot", "DB_SCHEMA_MAIN": "moonshot", "DB_SCHEMA_TAPE": "tape",
                "DATA_DIR": "/data", "TAPE_DIR": "/tape", "TAPE_ENABLED": "false", "JOB_BUCKET": bucket, "JOB_BUCKET_REGION": archive_region,
            },
            logging=ecs.LogDriver.aws_logs(stream_prefix="job", log_group=logs_group),
        )
        job.add_container_dependencies(ecs.ContainerDependency(container=db, condition=ecs.ContainerDependencyCondition.HEALTHY))

        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "TaskDefinition", value=task.family)
        CfnOutput(self, "SecurityGroup", value=sg.security_group_id)
        CfnOutput(self, "Subnets", value=",".join(s.subnet_id for s in vpc.public_subnets))
        CfnOutput(self, "LogGroup", value=logs_group.log_group_name)
