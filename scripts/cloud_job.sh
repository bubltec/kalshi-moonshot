#!/usr/bin/env bash
# Run one Python script on a throwaway Fargate task (infra/jobs_stack.py) instead of on the bot host, and print what it printed.
#   scripts/cloud_job.sh scripts/commodity_curve_model_walkforward.py
#   scripts/cloud_job.sh -m kalshi_moonshot.curvemodel          # the refit; the model file comes back in jobs/<id>/files/
#   scripts/cloud_job.sh scripts/some_study.py DAYS=5 HOURS=14  # NAME=value pairs are the script's environment
# The task restores the newest nightly backups (up to about a day old), so it suits refits and walk-forwards, not a review of the last few hours.
# It uses this Mac's data/strategies.toml and the archive bucket named in .env (ARCHIVE_BUCKET); results are copied to ./job-results/<id>/. SPOT=0 for on-demand capacity.
set -euo pipefail
cd "$(dirname "$0")/.."

region="${BOT_REGION:-us-east-2}"
stack="${JOBS_STACK:-KalshiMoonshotJobs}"
profile=()
if [ -n "${AWS_PROFILE:-}" ]; then profile=(--profile "$AWS_PROFILE"); fi
[ "$#" -ge 1 ] || { echo "usage: $0 SCRIPT.py|-m MODULE [NAME=value ...]" >&2; exit 2; }
bucket=$(grep -E '^ARCHIVE_BUCKET=' .env | head -1 | cut -d= -f2- | tr -d '"' || true)
[ -n "$bucket" ] || { echo "ARCHIVE_BUCKET is not set in .env" >&2; exit 1; }
archive_region=$(grep -E '^ARCHIVE_REGION=' .env | head -1 | cut -d= -f2- | tr -d '"' || true)
archive_region="${archive_region:-us-east-1}"

id="$(date +%Y%m%d-%H%M%S)-$$"
work=$(mktemp -d)
if [ "$1" = "-m" ]; then
  printf 'import runpy\nrunpy.run_module("%s", run_name="__main__")\n' "$2" > "$work/script.py"
  shift 2
else
  cp "$1" "$work/script.py"
  shift
fi
job_env="$*"
aws "${profile[@]}" s3 cp --region "$archive_region" --only-show-errors "$work/script.py" "s3://$bucket/jobs/$id/script.py"
aws "${profile[@]}" s3 cp --region "$archive_region" --only-show-errors data/strategies.toml "s3://$bucket/jobs/$id/strategies.toml"
rm -rf "$work"

out() { aws "${profile[@]}" cloudformation describe-stacks --region "$region" --stack-name "$stack" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
cluster=$(out ClusterName); taskdef=$(out TaskDefinition); sg=$(out SecurityGroup); subnets=$(out Subnets)
if [ "${SPOT:-1}" = 0 ]; then strategy=(--capacity-provider-strategy capacityProvider=FARGATE,weight=1); else strategy=(--capacity-provider-strategy capacityProvider=FARGATE_SPOT,weight=1); fi
overrides=$(JOB_ID="$id" JOB_ENV="$job_env" python3 -c 'import json,os; print(json.dumps({"containerOverrides":[{"name":"job","environment":[{"name":"JOB_ID","value":os.environ["JOB_ID"]},{"name":"JOB_ENV","value":os.environ["JOB_ENV"]}]}]}))')
task=$(aws "${profile[@]}" ecs run-task --region "$region" --cluster "$cluster" --task-definition "$taskdef" "${strategy[@]}" \
  --network-configuration "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$sg],assignPublicIp=ENABLED}" \
  --overrides "$overrides" --query 'tasks[0].taskArn' --output text)
echo "job $id started: $task"
status=""
while [ "$status" != "STOPPED" ]; do
  sleep 20
  status=$(aws "${profile[@]}" ecs describe-tasks --region "$region" --cluster "$cluster" --tasks "$task" --query 'tasks[0].lastStatus' --output text)
done
dest="job-results/$id"
mkdir -p "$dest"
aws "${profile[@]}" s3 sync --region "$archive_region" --only-show-errors "s3://$bucket/jobs/$id/" "$dest/" --exclude script.py --exclude strategies.toml || true
cat "$dest/out.txt" 2>/dev/null || echo "(no output: see the task's log group in CloudWatch)"
code=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("exit", 1))' "$dest/done.json" 2>/dev/null || echo 1)
echo "exit=$code  results in $dest/"
exit "$code"
