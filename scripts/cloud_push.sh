#!/usr/bin/env bash
# Send this Mac's lane file and .env to the Ohio host. The bot picks them up on its next decision.
#   scripts/cloud_push.sh            # strategies.toml, .env, and .secrets
#   scripts/cloud_push.sh --build    # that, then a new image and a container restart
# One copy of the bot should be running. Stop the local one before the cloud one trades.
set -euo pipefail
cd "$(dirname "$0")/.."

region="${BOT_REGION:-us-east-2}"
stack="${BOT_STACK:-KalshiMoonshotBot}"
profile=()
if [ -n "${AWS_PROFILE:-}" ]; then profile=(--profile "$AWS_PROFILE"); fi
build=0
if [ "${1:-}" = "--build" ]; then build=1; fi

put() {
  aws "${profile[@]}" ssm put-parameter --region "$region" --name "$1" --type SecureString --overwrite --value "file://$2" >/dev/null
}

[ -f .env ] || { echo "no .env in $(pwd)" >&2; exit 1; }
put /kalshi-moonshot/dotenv .env
if [ -f data/strategies.toml ]; then
  # A standard parameter holds 4 KB. The lane file is larger, so it is stored as base64 pieces under that limit.
  chunk_dir=$(mktemp -d)
  chunk_count=$(python3 -c 'import base64, pathlib, sys
raw = pathlib.Path("data/strategies.toml").read_bytes()
out = pathlib.Path(sys.argv[1])
size = 2970
parts = [raw[i:i + size] for i in range(0, len(raw), size)] or [b""]
for i, part in enumerate(parts):
    (out / f"{i:02d}").write_text(base64.b64encode(part).decode())
print(len(parts))' "$chunk_dir")
  i=0
  while [ "$i" -lt "$chunk_count" ]; do
    piece=$(printf '%02d' "$i")
    put "/kalshi-moonshot/strategies/$piece" "$chunk_dir/$piece"
    i=$((i + 1))
  done
  rm -rf "$chunk_dir"
  while :; do
    extra=$(printf '/kalshi-moonshot/strategies/%02d' "$i")
    aws "${profile[@]}" ssm delete-parameter --region "$region" --name "$extra" >/dev/null 2>&1 || break
    i=$((i + 1))
  done
fi
if [ -d .secrets ]; then
  for f in .secrets/*; do
    [ -f "$f" ] || continue
    put "/kalshi-moonshot/secret/$(basename "$f")" "$f"
  done
fi

if [ "$build" = 1 ]; then
  account=$(aws "${profile[@]}" sts get-caller-identity --query Account --output text)
  registry="${account}.dkr.ecr.${region}.amazonaws.com"
  aws "${profile[@]}" ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$registry"
  docker build --platform linux/arm64 -t "${registry}/kalshi-moonshot-bot:latest" .
  docker build --platform linux/arm64 -f Dockerfile.db -t "${registry}/kalshi-moonshot-db:latest" .
  docker push "${registry}/kalshi-moonshot-bot:latest"
  docker push "${registry}/kalshi-moonshot-db:latest"
fi

instance=$(aws "${profile[@]}" cloudformation describe-stacks --region "$region" --stack-name "$stack" \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)
[ -n "$instance" ] && [ "$instance" != "None" ] || { echo "stack $stack has no InstanceId in $region. Deploy it from infra/ first." >&2; exit 1; }

remote=$(cat << 'EOF'
set -euo pipefail
install -d -m 0700 /mnt/moonshot/env /mnt/moonshot/secrets /mnt/moonshot/data
aws ssm get-parameter --region REGION_HERE --name /kalshi-moonshot/dotenv --with-decryption --query Parameter.Value --output text > /mnt/moonshot/env/.env
chmod 0600 /mnt/moonshot/env/.env
tmp=$(mktemp)
: > "$tmp"
i=0
while :; do
  name=$(printf '/kalshi-moonshot/strategies/%02d' "$i")
  if ! val=$(aws ssm get-parameter --region REGION_HERE --name "$name" --with-decryption --query Parameter.Value --output text 2>/dev/null); then
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
names=$(aws ssm get-parameters-by-path --region REGION_HERE --path /kalshi-moonshot/secret/ --with-decryption --query 'Parameters[].Name' --output text || true)
for name in $names; do
  [ "$name" = "None" ] && continue
  base=$(basename "$name")
  aws ssm get-parameter --region REGION_HERE --name "$name" --with-decryption --query Parameter.Value --output text > "/mnt/moonshot/secrets/$base"
  chmod 0600 "/mnt/moonshot/secrets/$base"
done
# The bot runs as uid 1000. These files are written as root, so hand them to that user or the process cannot read .env.
chown -R 1000:1000 /mnt/moonshot/data /mnt/moonshot/secrets /mnt/moonshot/env/.env
if [ BUILD_HERE = 1 ]; then /usr/local/bin/moonshot-up; fi
echo synced
EOF
)
remote=${remote//REGION_HERE/$region}
remote=${remote//BUILD_HERE/$build}

params=$(REMOTE="$remote" python3 -c 'import json,os; print(json.dumps({"commands":[os.environ["REMOTE"]]}))')
cmd=$(aws "${profile[@]}" ssm send-command --region "$region" --instance-ids "$instance" \
  --document-name AWS-RunShellScript --comment "moonshot-sync" \
  --parameters "$params" --query Command.CommandId --output text)
aws "${profile[@]}" ssm wait command-executed --region "$region" --command-id "$cmd" --instance-id "$instance" || true
status=$(aws "${profile[@]}" ssm get-command-invocation --region "$region" --command-id "$cmd" --instance-id "$instance" \
  --query '[Status,StandardErrorContent]' --output text)
echo "$status"
case "$status" in
  Success*) echo "pushed to ${instance}" ;;
  *) exit 1 ;;
esac
