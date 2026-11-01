"""One machine in us-east-2 that runs the bot and its database. The model stays on the Mac.

The instance has no open ports. You reach it with Systems Manager. Its disk for Postgres and the
bot's data is a separate volume, so replacing the instance does not throw the tape away. The
container images live in this account's registry; scripts/cloud_push.sh builds them and starts
the containers. Nothing in this stack contains a key or a password.
"""
from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, RemovalPolicy, Size, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from constructs import Construct

COMPOSE = Path(__file__).with_name("compose.cloud.yml").read_text()


class BotStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = self.node.try_get_context
        instance_type = str(ctx("bot_instance") or "t4g.medium")
        volume_gb = int(ctx("bot_volume_gb") or 100)
        az = str(ctx("bot_az") or self.availability_zones[0])

        bot_repo = ecr.Repository(
            self, "BotRepo", repository_name="kalshi-moonshot-bot",
            image_scan_on_push=True, removal_policy=RemovalPolicy.RETAIN, empty_on_delete=False,
        )
        db_repo = ecr.Repository(
            self, "DbRepo", repository_name="kalshi-moonshot-db",
            image_scan_on_push=True, removal_policy=RemovalPolicy.RETAIN, empty_on_delete=False,
        )
        for repo in (bot_repo, db_repo):
            repo.add_lifecycle_rule(max_image_count=8)

        vpc = ec2.Vpc(
            self, "Vpc", max_azs=2, nat_gateways=0,
            subnet_configuration=[ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)],
        )
        sg = ec2.SecurityGroup(self, "HostSg", vpc=vpc, description="Bot host. No inbound; outbound only.", allow_all_outbound=True)

        role = iam.Role(self, "HostRole", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"))
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"))
        bot_repo.grant_pull(role)
        db_repo.grant_pull(role)
        role.add_to_policy(iam.PolicyStatement(
            sid="ReadTheBotEnv",
            actions=["ssm:GetParameter", "ssm:GetParametersByPath"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/kalshi-moonshot/*"],
        ))
        role.add_to_policy(iam.PolicyStatement(
            sid="DecryptThoseParameters",
            actions=["kms:Decrypt"],
            resources=["*"],
            conditions={"StringEquals": {"kms:ViaService": f"ssm.{self.region}.amazonaws.com"}},
        ))

        data = ec2.Volume(
            self, "Data", availability_zone=az, size=Size.gibibytes(volume_gb),
            encrypted=True, volume_type=ec2.EbsDeviceVolumeType.GP3,
            removal_policy=RemovalPolicy.RETAIN,
        )
        Tags.of(data).add("Name", "kalshi-moonshot-data")

        ud = ec2.UserData.for_linux()
        ud.add_commands(_bootstrap(self.region))
        ud.add_commands("echo VOL_ID=" + data.volume_id + " >> /usr/local/bin/moonshot-up")
        ud.add_commands("echo BOT_IMAGE=" + bot_repo.repository_uri + ":latest >> /usr/local/bin/moonshot-up")
        ud.add_commands("echo DB_IMAGE=" + db_repo.repository_uri + ":latest >> /usr/local/bin/moonshot-up")
        ud.add_commands(_write_files())
        host = ec2.Instance(
            self, "Host",
            instance_type=ec2.InstanceType(instance_type),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(cpu_type=ec2.AmazonLinuxCpuType.ARM_64),
            vpc=vpc,
            availability_zone=az,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC, availability_zones=[az]),
            security_group=sg,
            role=role,
            require_imdsv2=True,
            block_devices=[ec2.BlockDevice(
                device_name="/dev/xvda",
                volume=ec2.BlockDeviceVolume.ebs(20, encrypted=True, volume_type=ec2.EbsDeviceVolumeType.GP3),
            )],
            user_data=ud,
        )
        ec2.CfnVolumeAttachment(self, "AttachData", instance_id=host.instance_id, volume_id=data.volume_id, device="/dev/sdf")

        CfnOutput(self, "InstanceId", value=host.instance_id)
        CfnOutput(self, "BotImage", value=f"{bot_repo.repository_uri}:latest")
        CfnOutput(self, "DbImage", value=f"{db_repo.repository_uri}:latest")
        CfnOutput(self, "Region", value=self.region)


def _bootstrap(region: str) -> str:
    return f"""set -euo pipefail
dnf install -y docker awscli
systemctl enable --now docker
mkdir -p /usr/libexec/docker/cli-plugins /opt/moonshot /mnt/moonshot
curl -fsSL -o /usr/libexec/docker/cli-plugins/docker-compose https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-aarch64
chmod 0755 /usr/libexec/docker/cli-plugins/docker-compose
printf '%s\\n' '#!/bin/bash' 'set -euo pipefail' 'REGION="{region}"' > /usr/local/bin/moonshot-up
"""


def _write_files() -> str:
    """The rest of the start script, plus the compose file with the image names filled in from the lines above it."""
    tail = r"""cat >> /usr/local/bin/moonshot-up << 'UP'
serial="${VOL_ID//-/}"
disk=""
for _ in $(seq 1 60); do
  disk=$(lsblk -ndo NAME,SERIAL | awk -v s="$serial" '$2 ~ s { print "/dev/" $1; exit }')
  [ -n "$disk" ] && break
  sleep 5
done
[ -n "$disk" ]
if ! blkid "$disk" >/dev/null 2>&1; then mkfs.ext4 -L moonshot "$disk"; fi
grep -q ' /mnt/moonshot ' /etc/fstab || echo "LABEL=moonshot /mnt/moonshot ext4 defaults,nofail 0 2" >> /etc/fstab
mountpoint -q /mnt/moonshot || mount /mnt/moonshot
install -d -m 0700 /mnt/moonshot/env /mnt/moonshot/secrets /mnt/moonshot/data
install -d -m 0755 /mnt/moonshot/pgdata
aws ssm get-parameter --region "$REGION" --name /kalshi-moonshot/dotenv --with-decryption --query Parameter.Value --output text > /mnt/moonshot/env/.env
chmod 0600 /mnt/moonshot/env/.env
tmp=$(mktemp)
: > "$tmp"
i=0
while :; do
  name=$(printf '/kalshi-moonshot/strategies/%02d' "$i")
  if ! val=$(aws ssm get-parameter --region "$REGION" --name "$name" --with-decryption --query Parameter.Value --output text 2>/dev/null); then
    break
  fi
  printf '%s' "$val" | base64 -d >> "$tmp"
  i=$((i + 1))
done
if [ "$i" -gt 0 ]; then
  mv "$tmp" /mnt/moonshot/data/strategies.toml
  chmod 0644 /mnt/moonshot/data/strategies.toml
else
  rm -f "$tmp"
fi
names=$(aws ssm get-parameters-by-path --region "$REGION" --path /kalshi-moonshot/secret/ --with-decryption --query 'Parameters[].Name' --output text || true)
for name in $names; do
  [ "$name" = "None" ] && continue
  base=$(basename "$name")
  aws ssm get-parameter --region "$REGION" --name "$name" --with-decryption --query Parameter.Value --output text > "/mnt/moonshot/secrets/$base"
  chmod 0600 "/mnt/moonshot/secrets/$base"
done
chown -R 1000:1000 /mnt/moonshot/data /mnt/moonshot/secrets /mnt/moonshot/env/.env
registry="${BOT_IMAGE%%/*}"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$registry"
sed -e "s|BOT_IMAGE|$BOT_IMAGE|" -e "s|DB_IMAGE|$DB_IMAGE|" /opt/moonshot/compose.src.yml > /opt/moonshot/compose.yml
docker compose --env-file /mnt/moonshot/env/.env -f /opt/moonshot/compose.yml pull
docker compose --env-file /mnt/moonshot/env/.env -f /opt/moonshot/compose.yml up -d
UP
chmod 0755 /usr/local/bin/moonshot-up
cat > /opt/moonshot/compose.src.yml << 'COMPOSE'
""" + COMPOSE + """COMPOSE
"""
    return tail
