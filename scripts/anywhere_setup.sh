#!/usr/bin/env bash
# One-time setup for passwordless AWS from this Mac (IAM Roles Anywhere; see infra/anywhere_stack.py).
#
#   scripts/anywhere_setup.sh keys          # make the private CA and a client certificate in ~/.aws/anywhere (nothing leaves the Mac)
#   scripts/anywhere_setup.sh profile       # after the stack is deployed: write the ~/.aws/config profile "moonshot"
#   scripts/anywhere_setup.sh renew         # new client certificate from the same CA (it lasts one year)
#
# Needs openssl, and the AWS signing helper binary (`aws_signing_helper`) on PATH:
# https://docs.aws.amazon.com/rolesanywhere/latest/userguide/credential-helper.html
set -euo pipefail

dir="$HOME/.aws/anywhere"
cmd="${1:-}"
umask 077
mkdir -p "$dir"

client() {
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$dir/client.key"
  openssl req -new -key "$dir/client.key" -subj "/CN=kalshi-moonshot-mac" -out "$dir/client.csr"
  cat > "$dir/client.ext" <<EOF
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = clientAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
EOF
  openssl x509 -req -in "$dir/client.csr" -CA "$dir/ca.pem" -CAkey "$dir/ca.key" -CAcreateserial \
    -days 365 -sha256 -extfile "$dir/client.ext" -out "$dir/client.pem"
  rm -f "$dir/client.csr" "$dir/client.ext"
  chmod 600 "$dir"/*.key
  echo "client certificate: $dir/client.pem (expires $(openssl x509 -in "$dir/client.pem" -noout -enddate | cut -d= -f2))"
}

case "$cmd" in
  keys)
    if [ -e "$dir/ca.key" ]; then echo "a CA already exists in $dir; use 'renew' for a new client certificate" >&2; exit 1; fi
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$dir/ca.key"
    openssl req -new -x509 -key "$dir/ca.key" -sha256 -days 3650 -subj "/CN=kalshi-moonshot-mac-ca" \
      -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" -addext "subjectKeyIdentifier=hash" \
      -out "$dir/ca.pem"
    client
    echo
    echo "next: cd infra && cdk deploy BubbletechAnywhere --profile jjosefaws -c anywhere_ca_pem=$dir/ca.pem"
    echo "      then: scripts/anywhere_setup.sh profile"
    ;;
  renew)
    [ -e "$dir/ca.key" ] || { echo "no CA in $dir; run 'keys' first" >&2; exit 1; }
    client
    ;;
  profile)
    command -v aws_signing_helper >/dev/null || { echo "aws_signing_helper is not on PATH (see the link at the top of this script)" >&2; exit 1; }
    out() { aws --profile "${SSO_PROFILE:-jjosefaws}" cloudformation describe-stacks --region us-east-1 --stack-name BubbletechAnywhere \
              --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
    anchor=$(out TrustAnchorArn); prof=$(out ProfileArn); role=$(out MoonshotRoleArn)
    cfg="$HOME/.aws/config"
    if grep -q '^\[profile moonshot\]' "$cfg" 2>/dev/null; then  # replace an older block (a redeploy changes the ARNs)
      cp "$cfg" "$cfg.bak"
      python3 - "$cfg" <<'PY'
import re, sys
p = sys.argv[1]
t = open(p).read()
open(p, "w").write(re.sub(r"\n?\[profile moonshot\]\n(?:(?!\[).*\n?)*", "\n", t))
PY
      echo "replaced the old [profile moonshot] (backup: $cfg.bak)"
    fi
    helper=$(command -v aws_signing_helper)
    cat >> "$cfg" <<EOF

[profile moonshot]
credential_process = $helper credential-process --certificate $dir/client.pem --private-key $dir/client.key --trust-anchor-arn $anchor --profile-arn $prof --role-arn $role
region = us-east-2
output = json
EOF
    echo "added [profile moonshot]. Try: AWS_PROFILE=moonshot aws sts get-caller-identity"
    ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
