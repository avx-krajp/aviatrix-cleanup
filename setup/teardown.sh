#!/usr/bin/env bash
# =============================================================================
# setup/teardown.sh — tear down an isolated Aviatrix Cloud Cleanup deploy.
#
# `sam delete` alone fails here: CloudFormation refuses to delete a non-empty
# S3 bucket, and this stack's web bucket always has files in it after deploy.
# There's also state that isn't a stack resource at all (the EventBridge
# schedule created imperatively by the app, the Azure layer staging bucket,
# and the Azure/GCP credential secrets — all created imperatively by
# setup/deploy.sh via `aws secretsmanager create-secret`, so CloudFormation
# has no record of them) that sam delete never touches.
#
# This script does the full sequence so nobody has to remember it by hand:
#   1. Delete the EventBridge Scheduler entry, if one was ever created.
#   2. Empty (then let sam delete remove) the web bucket.
#   3. Remove the Azure layer staging bucket, if one was created.
#   4. Delete the Azure/GCP credential secrets, if any were created.
#   5. Run sam delete.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

R='\033[0;31m'; Y='\033[1;33m'; G='\033[0;32m'; B='\033[1m'; N='\033[0m'

step() { printf "\n${B}==> %s${N}\n" "$1"; }
ok()   { printf "${G}✓${N} %s\n" "$1"; }
warn() { printf "${Y}!${N} %s\n" "$1"; }
err()  { printf "${R}✗ %s${N}\n" "$1" >&2; }

if [ $# -ne 1 ]; then
  err "Usage: $0 <username>"
  echo "  This is the same username you gave setup/deploy.sh, e.g. 'kraj' -> stack 'aviatrix-cleanup-kraj'."
  exit 1
fi

PREFIX_SLUG="$1"
RESOURCE_PREFIX="${PREFIX_SLUG}-"
STACK_NAME="aviatrix-cleanup-${PREFIX_SLUG}"
CONFIG_FILE="samconfig.${PREFIX_SLUG}.toml"

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  err "AWS credentials are not configured or have expired."
  echo "Run 'aws configure' or refresh your SSO session, then re-run this script."
  exit 1
fi
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region 2>/dev/null || echo "us-east-1")
ok "AWS credentials OK — account $ACCOUNT_ID, region $REGION"
ok "Tearing down stack '$STACK_NAME'"

step "Deleting EventBridge schedule (if any)"
SCHEDULE_NAME="${RESOURCE_PREFIX}aviatrix-cleanup-stop-daily"
if aws scheduler get-schedule --name "$SCHEDULE_NAME" --group-name default >/dev/null 2>&1; then
  aws scheduler delete-schedule --name "$SCHEDULE_NAME" --group-name default >/dev/null
  ok "Deleted schedule $SCHEDULE_NAME"
else
  ok "No schedule $SCHEDULE_NAME found — nothing to do"
fi

step "Emptying web bucket"
WEB_BUCKET="${RESOURCE_PREFIX}aviatrix-cleanup-web-${ACCOUNT_ID}"
if aws s3api head-bucket --bucket "$WEB_BUCKET" --region "$REGION" >/dev/null 2>&1; then
  aws s3 rm "s3://$WEB_BUCKET/" --recursive >/dev/null
  ok "Emptied $WEB_BUCKET — sam delete can now remove the bucket itself"
else
  ok "Bucket $WEB_BUCKET not found — nothing to do"
fi

step "Removing Azure SDK layer staging bucket (if any)"
LAYER_STAGING_BUCKET="${RESOURCE_PREFIX}aviatrix-cleanup-layer-staging-${ACCOUNT_ID}"
if aws s3api head-bucket --bucket "$LAYER_STAGING_BUCKET" --region "$REGION" >/dev/null 2>&1; then
  aws s3 rb "s3://$LAYER_STAGING_BUCKET" --force >/dev/null
  ok "Removed $LAYER_STAGING_BUCKET"
else
  ok "Bucket $LAYER_STAGING_BUCKET not found — nothing to do"
fi

step "Deleting credential secrets (if any)"
for SECRET_NAME in "${RESOURCE_PREFIX}aviatrix-cleanup/azure-sp" "${RESOURCE_PREFIX}aviatrix-cleanup/gcp-sa"; do
  if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" >/dev/null 2>&1; then
    aws secretsmanager delete-secret --secret-id "$SECRET_NAME" --region "$REGION" \
      --force-delete-without-recovery >/dev/null
    ok "Deleted secret $SECRET_NAME"
  else
    ok "Secret $SECRET_NAME not found — nothing to do"
  fi
done

step "Deleting CloudFormation stack"
if [ ! -f "$CONFIG_FILE" ]; then
  warn "Config file $CONFIG_FILE not found in $REPO_ROOT — passing --stack-name/--region directly instead."
  sam delete --stack-name "$STACK_NAME" --region "$REGION" --no-prompts
else
  sam delete --stack-name "$STACK_NAME" --config-file "$CONFIG_FILE" --region "$REGION" --no-prompts
fi

ok "Stack '$STACK_NAME' deleted"
