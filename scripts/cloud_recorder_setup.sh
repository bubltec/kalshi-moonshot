#!/usr/bin/env bash
# One-time: add the tape recorder container to the running host IN PLACE (no new instance, nothing on the data volume touched).
#   1. scripts/cloud_push.sh --build --recorder     # the image, under the :latest and :recorder tags
#   2. scripts/cloud_recorder_setup.sh              # the compose file on the host, then moonshot-up
# It rewrites /opt/moonshot/compose.src.yml from infra/compose.cloud.yml (a copy of the old file is kept beside it), teaches /usr/local/bin/moonshot-up to fill in the recorder's image, and runs it once. The bot is recreated once, in the memory role, as the recorder starts; after that a bot update leaves the recorder alone.
# The CDK stack (infra/bot_stack.py) carries the same change for a machine built from scratch; do not `cdk deploy` just for this: a changed start script replaces the instance.
set -euo pipefail
cd "$(dirname "$0")/.."
region="${BOT_REGION:-us-east-2}"
stack="${BOT_STACK:-KalshiMoonshotBot}"
profile=()
if [ -n "${AWS_PROFILE:-}" ]; then profile=(--profile "$AWS_PROFILE"); fi
instance=$(aws "${profile[@]}" cloudformation describe-stacks --region "$region" --stack-name "$stack" --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)
blob=$(gzip -c infra/compose.cloud.yml | base64 | tr -d '\n')
remote=$(cat << EOS
set -euo pipefail
cp /opt/moonshot/compose.src.yml /opt/moonshot/compose.src.yml.before-recorder
echo $blob | base64 -d | gunzip > /opt/moonshot/compose.src.yml
if ! grep -q 'RECORDER_IMAGE' /usr/local/bin/moonshot-up; then
  cp /usr/local/bin/moonshot-up /usr/local/bin/moonshot-up.before-recorder
  sed -i 's#-e "s|DB_IMAGE|\$DB_IMAGE|"#-e "s|DB_IMAGE|\$DB_IMAGE|" -e "s|RECORDER_IMAGE|\${BOT_IMAGE%:latest}:recorder|"#' /usr/local/bin/moonshot-up
fi
grep -c 'RECORDER_IMAGE' /usr/local/bin/moonshot-up
/usr/local/bin/moonshot-up
docker ps --format '{{.Names}}  {{.Status}}'
EOS
)
params=$(REMOTE="$remote" python3 -c 'import json,os; print(json.dumps({"commands":[os.environ["REMOTE"]]}))')
cmd=$(aws "${profile[@]}" ssm send-command --region "$region" --instance-ids "$instance" --document-name AWS-RunShellScript --comment "moonshot-recorder-setup" --parameters "$params" --query Command.CommandId --output text)
aws "${profile[@]}" ssm wait command-executed --region "$region" --command-id "$cmd" --instance-id "$instance" || true
aws "${profile[@]}" ssm get-command-invocation --region "$region" --command-id "$cmd" --instance-id "$instance" --query '[Status,StandardOutputContent,StandardErrorContent]' --output text
