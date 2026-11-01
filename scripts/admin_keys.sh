#!/usr/bin/env bash
# Make the key pair CloudFront uses to check the admin login's signed cookies. Run once, then deploy the site stack.
#   scripts/admin_keys.sh
# The private half goes straight to SSM (SecureString, us-east-1) and is never written anywhere else; the temp copy is deleted. The public half goes to
# infra/admin_cf_public_key.pem (not a secret; commit it). Run it again to rotate: deploy the site stack afterwards, then sign in again.
set -euo pipefail
cd "$(dirname "$0")/.."

region="${SITE_REGION:-us-east-1}"
profile=()
if [ -n "${AWS_PROFILE:-}" ]; then profile=(--profile "$AWS_PROFILE"); fi
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

openssl genrsa -out "$tmp/private.pem" 2048 2>/dev/null
openssl rsa -in "$tmp/private.pem" -pubout -out infra/admin_cf_public_key.pem 2>/dev/null
aws "${profile[@]}" ssm put-parameter --region "$region" --name /kalshi-moonshot/admin/cf-private-key --type SecureString --overwrite --value "file://$tmp/private.pem" >/dev/null
echo "private key stored in SSM /kalshi-moonshot/admin/cf-private-key; public key written to infra/admin_cf_public_key.pem"
