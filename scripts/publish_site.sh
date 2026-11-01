#!/usr/bin/env bash
# Put the public page on kalshi-moonshot.bubbletech.io. Needs the KalshiMoonshotSite stack deployed (infra/site_stack.py) and the AWS CLI signed in.
#   scripts/publish_site.sh static            # build public-site and upload the page (index.html and assets/); public.json is left alone
#   scripts/publish_site.sh data [FILE]       # check FILE (default site-data/public.json) against the allowlist, then upload it as public.json
#   scripts/publish_site.sh all [FILE]        # both
#   scripts/publish_site.sh admin             # build admin-site and upload it to admin/ (index.html and assets/); admin.json is written only by the bot
# Only public/ (and admin/ for the admin page) of the bucket is written, and the only data file that can go up is a public.json that passes `exporter --check`.
# admin.json is refused by name. AWS_PROFILE works as in cloud_push.sh; BOT_SITE_STACK and SITE_REGION override the stack name and region.
set -euo pipefail
cd "$(dirname "$0")/.."

region="${SITE_REGION:-us-east-1}"
stack="${BOT_SITE_STACK:-KalshiMoonshotSite}"
profile=()
if [ -n "${AWS_PROFILE:-}" ]; then profile=(--profile "$AWS_PROFILE"); fi
aws_() { aws "${profile[@]}" "$@"; }
output() { aws_ cloudformation describe-stacks --region "$region" --stack-name "$stack" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

mode="${1:-}"; file="${2:-site-data/public.json}"
case "$mode" in static | data | all | admin) ;; *) sed -n 2,6p "$0" >&2; exit 2 ;; esac
bucket=$(output BucketName); dist=$(output DistributionId)
[ -n "$bucket" ] && [ "$bucket" != "None" ] || { echo "no site stack: deploy KalshiMoonshotSite from infra/ first" >&2; exit 1; }

if [ "$mode" = static ] || [ "$mode" = all ]; then
  pnpm --dir public-site install --frozen-lockfile
  pnpm --dir public-site build
  aws_ s3 sync public-site/dist "s3://$bucket/public/" --delete --exclude "public.json" --exclude "assets/*" --cache-control "no-cache" --region "$region"
  aws_ s3 sync public-site/dist/assets "s3://$bucket/public/assets/" --delete --cache-control "public, max-age=31536000, immutable" --region "$region"
  aws_ cloudfront create-invalidation --distribution-id "$dist" --paths "/index.html" "/" >/dev/null
  echo "page uploaded"
fi

if [ "$mode" = admin ]; then
  pnpm --dir admin-site install --frozen-lockfile
  pnpm --dir admin-site build
  aws_ s3 sync admin-site/dist "s3://$bucket/admin/" --delete --exclude "admin.json" --exclude "assets/*" --cache-control "no-store" --region "$region"
  aws_ s3 sync admin-site/dist/assets "s3://$bucket/admin/assets/" --delete --cache-control "private, max-age=31536000, immutable" --region "$region"
  echo "admin page uploaded: https://kalshi-moonshot.bubbletech.io/admin/ (sign in at /auth/github)"
fi

if [ "$mode" = data ] || [ "$mode" = all ]; then
  [ "$(basename "$file")" = public.json ] || { echo "only a file named public.json can be published (got $file)" >&2; exit 1; }
  [ -f "$file" ] || { echo "$file not found: write it with  python -m kalshi_moonshot.exporter --out site-data" >&2; exit 1; }
  .venv/bin/python -m kalshi_moonshot.exporter --check "$file"
  aws_ s3 cp "$file" "s3://$bucket/public/public.json" --content-type application/json --cache-control "public, max-age=60" --region "$region"
  aws_ cloudfront create-invalidation --distribution-id "$dist" --paths "/public.json" >/dev/null
  echo "public.json uploaded"
fi
echo "https://kalshi-moonshot.bubbletech.io"
