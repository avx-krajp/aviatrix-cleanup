#!/usr/bin/env bash
# =============================================================================
# setup/deploy.sh — guided first-time deploy for Aviatrix Cloud Cleanup.
#
# Auto-installs the AWS CLI and AWS SAM CLI on macOS (via Homebrew) if
# missing, checks your AWS credentials, optionally creates the Azure/GCP
# Secrets Manager secrets, generates the auth signing key, prompts for a
# login passphrase, then runs `sam build && sam deploy` with everything
# wired up.
#
# Safe to re-run — it detects an existing stack and skips the --guided
# wizard on updates, reusing samconfig.toml and any resources that already
# exist (secrets, etc).
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

# ── 1. Check prerequisites (auto-install on macOS via Homebrew) ─────────────
step "Checking prerequisites"

if [[ "$(uname -s)" == "Darwin" ]]; then
  if ! command -v brew >/dev/null 2>&1; then
    err "Homebrew not found. Install it first: https://brew.sh"
    exit 1
  fi

  if ! command -v aws >/dev/null 2>&1; then
    warn "AWS CLI not found — installing via Homebrew..."
    brew install awscli
  fi

  if ! command -v sam >/dev/null 2>&1; then
    warn "AWS SAM CLI not found — installing via Homebrew..."
    brew install aws-sam-cli
  fi

  if ! command -v node >/dev/null 2>&1; then
    warn "Node.js not found — installing via Homebrew..."
    brew install node
  fi
else
  command -v aws >/dev/null 2>&1 || { err "AWS CLI not found. Install it first."; exit 1; }
  command -v sam >/dev/null 2>&1 || { err "AWS SAM CLI not found. Install it first."; exit 1; }
  command -v node >/dev/null 2>&1 || { err "Node.js not found. Install it first."; exit 1; }
fi

command -v aws >/dev/null 2>&1 || { err "AWS CLI install failed — install it manually and re-run."; exit 1; }
command -v sam >/dev/null 2>&1 || { err "AWS SAM CLI install failed — install it manually and re-run."; exit 1; }
command -v node >/dev/null 2>&1 || { err "Node.js install failed — install it manually and re-run."; exit 1; }
ok "aws, sam and node found"

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  err "AWS credentials are not configured or have expired."
  echo "Run 'aws configure' or refresh your SSO session, then re-run this script."
  exit 1
fi
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region 2>/dev/null || echo "us-east-1")
ok "AWS credentials OK — account $ACCOUNT_ID, region $REGION"

STACK_NAME="aviatrix-cleanup"
if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
  FIRST_DEPLOY=false
  ok "Existing stack '$STACK_NAME' found in $REGION — this will update it."
else
  FIRST_DEPLOY=true
fi

# ── 2. Login passphrase + signing key ───────────────────────────────────────
step "Web login setup"

LOGIN_PASSWORD=""
while [ ${#LOGIN_PASSWORD} -lt 12 ]; do
  read -rsp "Choose a login passphrase for the web app (min 12 chars): " LOGIN_PASSWORD
  echo
  [ ${#LOGIN_PASSWORD} -lt 12 ] && warn "Too short (${#LOGIN_PASSWORD} chars) — try again."
done
ok "Passphrase set"

AUTH_SIGNING_KEY=$(openssl rand -hex 32)
ok "Generated auth signing key"

# ── 3. Optional Azure secret ─────────────────────────────────────────────────
step "Azure cleanup + instances support (optional)"

AZURE_SP_SECRET_ARN=""
AZURE_SDK_LAYER_ARN=""
read -rp "Enable Azure support? [y/N] " enable_azure
if [[ "$enable_azure" =~ ^[Yy]$ ]]; then
  read -rp "  Already have a Secrets Manager ARN to reuse (e.g. a shared org secret)? Leave blank to create/update aviatrix-cleanup/azure-sp: " AZURE_SP_SECRET_ARN

  if [ -z "$AZURE_SP_SECRET_ARN" ]; then
    read -rp "  Azure tenant ID: " AZ_TENANT_ID
    read -rp "  Azure client (app) ID: " AZ_CLIENT_ID
    read -rsp "  Azure client secret: " AZ_CLIENT_SECRET; echo
    read -rp "  Azure subscription ID: " AZ_SUB_ID

    SECRET_JSON=$(printf '{"tenantId":"%s","clientId":"%s","clientSecret":"%s","subscriptionId":"%s"}' \
      "$AZ_TENANT_ID" "$AZ_CLIENT_ID" "$AZ_CLIENT_SECRET" "$AZ_SUB_ID")

    if AZURE_SP_SECRET_ARN=$(aws secretsmanager describe-secret --secret-id aviatrix-cleanup/azure-sp --query ARN --output text 2>/dev/null); then
      warn "Secret aviatrix-cleanup/azure-sp already exists — updating it."
      aws secretsmanager put-secret-value --secret-id aviatrix-cleanup/azure-sp --secret-string "$SECRET_JSON" >/dev/null
    else
      AZURE_SP_SECRET_ARN=$(aws secretsmanager create-secret \
        --name aviatrix-cleanup/azure-sp \
        --secret-string "$SECRET_JSON" \
        --query ARN --output text)
    fi
  fi
  ok "Azure secret ready: $AZURE_SP_SECRET_ARN"

  read -rp "  Azure SDK Lambda layer ARN (see docs/AZURE_LAYER.md if you don't have one yet, or leave blank to skip Azure for now): " AZURE_SDK_LAYER_ARN
  if [ -z "$AZURE_SDK_LAYER_ARN" ]; then
    warn "No layer ARN provided — Azure cleanup/instances will error until you build one and redeploy with AzureSdkLayerArn set."
  fi
else
  ok "Skipping Azure — AzureSpSecretArn left blank"
fi

# ── 4. Optional GCP secret ───────────────────────────────────────────────────
step "GCP cleanup + instances support (optional)"

GCP_SA_SECRET_ARN=""
read -rp "Enable GCP support? [y/N] " enable_gcp
if [[ "$enable_gcp" =~ ^[Yy]$ ]]; then
  read -rp "  Already have a Secrets Manager ARN to reuse (e.g. a shared org secret)? Leave blank to create/update aviatrix-cleanup/gcp-sa: " GCP_SA_SECRET_ARN

  if [ -z "$GCP_SA_SECRET_ARN" ]; then
    read -rp "  Path to your GCP service account key JSON file: " GCP_KEY_PATH
    if [ ! -f "$GCP_KEY_PATH" ]; then
      err "File not found: $GCP_KEY_PATH"
      exit 1
    fi
    if GCP_SA_SECRET_ARN=$(aws secretsmanager describe-secret --secret-id aviatrix-cleanup/gcp-sa --query ARN --output text 2>/dev/null); then
      warn "Secret aviatrix-cleanup/gcp-sa already exists — updating it."
      aws secretsmanager put-secret-value --secret-id aviatrix-cleanup/gcp-sa --secret-string "file://$GCP_KEY_PATH" >/dev/null
    else
      GCP_SA_SECRET_ARN=$(aws secretsmanager create-secret \
        --name aviatrix-cleanup/gcp-sa \
        --secret-string "file://$GCP_KEY_PATH" \
        --query ARN --output text)
    fi
  fi
  ok "GCP secret ready: $GCP_SA_SECRET_ARN"
else
  ok "Skipping GCP — GcpSaSecretArn left blank"
fi

# ── 5. Build ──────────────────────────────────────────────────────────────────
step "Building"
sam build

# ── 6. Deploy ─────────────────────────────────────────────────────────────────
step "Deploying"

OVERRIDES="StageName=prod LoginPassword=\"$LOGIN_PASSWORD\" AuthSigningKey=\"$AUTH_SIGNING_KEY\""
[ -n "$AZURE_SP_SECRET_ARN" ] && OVERRIDES="$OVERRIDES AzureSpSecretArn=\"$AZURE_SP_SECRET_ARN\""
[ -n "$AZURE_SDK_LAYER_ARN" ] && OVERRIDES="$OVERRIDES AzureSdkLayerArn=\"$AZURE_SDK_LAYER_ARN\""
[ -n "$GCP_SA_SECRET_ARN" ]   && OVERRIDES="$OVERRIDES GcpSaSecretArn=\"$GCP_SA_SECRET_ARN\""

if $FIRST_DEPLOY; then
  # First deploy: run the full wizard once so stack name/region/capabilities
  # get saved to samconfig.toml for every future run.
  eval sam deploy --guided --region "$REGION" --parameter-overrides "$OVERRIDES"
else
  # Re-run: reuse the saved samconfig.toml settings, only override parameters.
  eval sam deploy --region "$REGION" --parameter-overrides "$OVERRIDES"
fi

# ── 7. Build and upload the web app ─────────────────────────────────────────
step "Building web app"

(cd web && npm install && npm run build)
ok "Web app built"

step "Uploading web app"

WEB_BUCKET=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='WebBucketName'].OutputValue" --output text)
DIST_ID=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDistributionId'].OutputValue" --output text)
WEB_URL=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='WebUrl'].OutputValue" --output text)

aws s3 sync ./web/dist/ "s3://$WEB_BUCKET/" --delete
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths '/*' >/dev/null
ok "Web app uploaded"

printf "\n${G}${B}Done.${N} Open %s and log in with the passphrase you set.\n\n" "$WEB_URL"
