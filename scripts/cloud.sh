#!/usr/bin/env bash
# Reach the Ohio host without typing an instance id. Everything goes through Systems Manager (no open ports).
#   scripts/cloud.sh status                      # machine, containers, last log lines
#   scripts/cloud.sh logs [N]                    # the bot's last N log lines (default 200), then follow
#   scripts/cloud.sh psql                        # an interactive psql on the cloud database
#   scripts/cloud.sh sql "SELECT count(*) FROM tape.quotes"   # one query, results printed
#   scripts/cloud.sh run scripts/some_study.py [VAR=value ...]   # a study from this repo, run against the CLOUD tape and trades
#   scripts/cloud.sh shell                       # a shell on the host
# `run` is the cloud twin of: docker compose exec -T bot python - < scripts/some_study.py
# Needs the AWS CLI signed in and the session-manager-plugin. AWS_PROFILE and BOT_REGION work as in cloud_push.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

region="${BOT_REGION:-us-east-2}"
stack="${BOT_STACK:-KalshiMoonshotBot}"
profile=()
if [ -n "${AWS_PROFILE:-}" ]; then profile=(--profile "$AWS_PROFILE"); fi
aws_() { aws "${profile[@]}" "$@"; }

instance="${BOT_INSTANCE:-}"
if [ -z "$instance" ]; then
  instance=$(aws_ cloudformation describe-stacks --region "$region" --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)
fi
[ -n "$instance" ] && [ "$instance" != "None" ] || { echo "no instance: deploy the stack from infra/ or set BOT_INSTANCE" >&2; exit 1; }

# a command whose whole output streams back (no 24 KB cap)
stream() {
  local params
  params=$(CMD="$1" python3 -c 'import json,os; print(json.dumps({"command": [os.environ["CMD"]]}))')
  if [ -t 1 ] || [ "${2:-}" = "tty" ]; then
    aws_ ssm start-session --region "$region" --target "$instance" --document-name AWS-StartInteractiveCommand --parameters "$params"
  else  # piped: drop the session banner lines so the output is only the command's
    aws_ ssm start-session --region "$region" --target "$instance" --document-name AWS-StartInteractiveCommand --parameters "$params" | sed -e '/^Starting session with SessionId/d' -e '/^Exiting session with sessionId/d'
  fi
}

# a short command whose output comes back at the end
remote() {
  local params cmd
  params=$(CMD="$1" python3 -c 'import json,os; print(json.dumps({"commands": [os.environ["CMD"]]}))')
  cmd=$(aws_ ssm send-command --region "$region" --instance-ids "$instance" --document-name AWS-RunShellScript --parameters "$params" --query Command.CommandId --output text)
  aws_ ssm wait command-executed --region "$region" --command-id "$cmd" --instance-id "$instance" 2>/dev/null || true
  aws_ ssm get-command-invocation --region "$region" --command-id "$cmd" --instance-id "$instance" \
    --query '[Status,StandardOutputContent,StandardErrorContent]' --output text
}

bot='$(sudo docker ps -qf name=bot | head -1)'
db='$(sudo docker ps -qf name=db | head -1)'
pg='psql -U moonshot -d moonshot'

case "${1:-}" in
  status)
    remote "uptime; free -m | sed -n 1,2p; df -h / /mnt/moonshot | tail -2; echo; sudo docker ps -a --format '{{.Names}}  {{.Status}}'; echo; sudo docker logs --tail 8 $bot 2>&1 | cut -c1-200"
    ;;
  logs)
    stream "sudo docker logs --tail ${2:-200} -f $bot 2>&1"
    ;;
  psql)
    stream "sudo docker exec -it $db $pg"
    ;;
  sql)
    [ -n "${2:-}" ] || { echo "usage: cloud.sh sql \"SELECT ...\"" >&2; exit 2; }
    q=$(printf '%s' "$2" | base64 | tr -d '\n')
    stream "echo $q | base64 -d | sudo docker exec -i $db $pg"
    ;;
  run)
    script="${2:-}"
    [ -f "$script" ] || { echo "usage: cloud.sh run scripts/some_study.py [VAR=value ...]" >&2; exit 2; }
    shift 2
    envs=""
    for kv in "$@"; do
      case "$kv" in *=*) envs="$envs $(printf '%q' "$kv")" ;; *) echo "not VAR=value: $kv" >&2; exit 2 ;; esac
    done
    blob=$(gzip -c "$script" | base64 | tr -d '\n')
    out=$(remote "echo $blob | base64 -d | gunzip > /tmp/cloud_run.py && wc -c /tmp/cloud_run.py")
    case "$out" in Success*) ;; *) echo "$out" >&2; exit 1 ;; esac
    stream "sudo docker exec -i $bot env$envs python - < /tmp/cloud_run.py"
    ;;
  shell)
    aws_ ssm start-session --region "$region" --target "$instance"
    ;;
  *)
    sed -n 2,11p "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
