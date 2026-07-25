#!/usr/bin/env bash
# =============================================================================
# setup/deploy.sh — guided deploy for Aviatrix Cloud Cleanup.
#
# Auto-installs the AWS CLI, AWS SAM CLI and Node.js on macOS (via Homebrew)
# if missing, checks your AWS credentials, optionally creates the Azure/GCP
# Secrets Manager secrets, auto-builds and publishes the Azure SDK Lambda
# layer if Azure support is enabled (reusing an existing published version
# if one is found), generates the auth signing key, prompts for a login
# passphrase, builds the web app, then runs `sam build && sam deploy` with
# everything wired up.
#
# Requires a username (e.g. yours) to deploy your own fully isolated copy —
# separate CloudFormation stack, DynamoDB tables, Lambdas, S3 bucket,
# CloudFront distribution, login passphrase and (optionally) Azure/GCP
# secrets, saved to its own samconfig.<username>.toml (gitignored). There is
# no shared/blank stack option — every deploy is isolated.
#
# Safe to re-run — it detects an existing stack (per prefix) and skips the
# --guided wizard on updates, reusing that stack's saved config file and any
# resources that already exist (secrets, etc).
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

  if ! command -v python3.12 >/dev/null 2>&1; then
    warn "python3.12 not found — installing via Homebrew (sam build needs this exact version to build the Lambda functions; template.yaml pins Runtime: python3.12)..."
    brew install python@3.12
  fi

  if ! command -v pip3 >/dev/null 2>&1; then
    warn "pip3 not found — installing via Homebrew..."
    brew install python3
  fi

  if ! command -v zip >/dev/null 2>&1; then
    warn "zip not found — installing via Homebrew..."
    brew install zip
  fi
else
  command -v aws >/dev/null 2>&1 || { err "AWS CLI not found. Install it first."; exit 1; }
  command -v sam >/dev/null 2>&1 || { err "AWS SAM CLI not found. Install it first."; exit 1; }
  command -v node >/dev/null 2>&1 || { err "Node.js not found. Install it first."; exit 1; }
  command -v python3.12 >/dev/null 2>&1 || { err "python3.12 not found (sam build needs this exact version — template.yaml pins Runtime: python3.12). Install it first."; exit 1; }
  command -v pip3 >/dev/null 2>&1 || { err "pip3 not found. Install it first."; exit 1; }
  command -v zip >/dev/null 2>&1 || { err "zip not found. Install it first."; exit 1; }
fi

command -v aws >/dev/null 2>&1 || { err "AWS CLI install failed — install it manually and re-run."; exit 1; }
command -v sam >/dev/null 2>&1 || { err "AWS SAM CLI install failed — install it manually and re-run."; exit 1; }
command -v node >/dev/null 2>&1 || { err "Node.js install failed — install it manually and re-run."; exit 1; }
command -v python3.12 >/dev/null 2>&1 || { err "python3.12 install failed — install it manually (e.g. 'brew install python@3.12' or 'pyenv install 3.12' and put it on PATH) and re-run."; exit 1; }
command -v pip3 >/dev/null 2>&1 || { err "pip3 install failed — install it manually and re-run."; exit 1; }
command -v zip >/dev/null 2>&1 || { err "zip install failed — install it manually and re-run."; exit 1; }
ok "aws, sam, node, python3.12 and zip found"

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  err "AWS credentials are not configured or have expired."
  echo "Run 'aws configure' or refresh your SSO session, then re-run this script."
  exit 1
fi
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region 2>/dev/null || echo "us-east-1")
ok "AWS credentials OK — account $ACCOUNT_ID, region $REGION"

# ── 1.5. Isolated deploy username ────────────────────────────────────────────
step "Deploy target"

PREFIX_SLUG=""
while [ -z "$PREFIX_SLUG" ]; do
  read -rp "Enter your username (letters/numbers/hyphens only, e.g. 'livaraj' -> stack 'aviatrix-cleanup-livaraj'): " RAW_PREFIX
  PREFIX_SLUG=$(echo "$RAW_PREFIX" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9-]/-/g; s/-+/-/g; s/^-//; s/-$//' | cut -c1-20)
  [ -z "$PREFIX_SLUG" ] && err "Username is required. Please enter a value."
done

RESOURCE_PREFIX="${PREFIX_SLUG}-"
STACK_NAME="aviatrix-cleanup-${PREFIX_SLUG}"
CONFIG_FILE="samconfig.${PREFIX_SLUG}.toml"
ok "Isolated deploy — stack '$STACK_NAME' (underlying resources prefixed '${RESOURCE_PREFIX}', e.g. '${RESOURCE_PREFIX}aviatrix-cleanup-jobs')"

if [ -f "$CONFIG_FILE" ] && aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
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
AZURE_SECRET_NAME="${RESOURCE_PREFIX}aviatrix-cleanup/azure-sp"
read -rp "Enable Azure support? [y/N] " enable_azure
if [[ "$enable_azure" =~ ^[Yy]$ ]]; then
  read -rp "  Already have a Secrets Manager ARN to reuse (e.g. a shared org secret)? Leave blank to create/update $AZURE_SECRET_NAME: " AZURE_SP_SECRET_ARN

  if [ -z "$AZURE_SP_SECRET_ARN" ]; then
    read -rp "  Azure tenant ID: " AZ_TENANT_ID
    read -rp "  Azure client (app) ID: " AZ_CLIENT_ID
    read -rsp "  Azure client secret: " AZ_CLIENT_SECRET; echo
    read -rp "  Azure subscription ID: " AZ_SUB_ID

    SECRET_JSON=$(printf '{"tenantId":"%s","clientId":"%s","clientSecret":"%s","subscriptionId":"%s"}' \
      "$AZ_TENANT_ID" "$AZ_CLIENT_ID" "$AZ_CLIENT_SECRET" "$AZ_SUB_ID")

    if AZURE_SP_SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "$AZURE_SECRET_NAME" --query ARN --output text 2>/dev/null); then
      warn "Secret $AZURE_SECRET_NAME already exists — updating it."
      aws secretsmanager put-secret-value --secret-id "$AZURE_SECRET_NAME" --secret-string "$SECRET_JSON" >/dev/null
    else
      AZURE_SP_SECRET_ARN=$(aws secretsmanager create-secret \
        --name "$AZURE_SECRET_NAME" \
        --secret-string "$SECRET_JSON" \
        --query ARN --output text)
    fi
  fi
  ok "Azure secret ready: $AZURE_SP_SECRET_ARN"

  AZURE_LAYER_NAME="${RESOURCE_PREFIX}aviatrix-cleanup-azure-sdk"
  echo "  Looking for an existing Azure SDK Lambda layer '$AZURE_LAYER_NAME' in this account..."
  if AZURE_SDK_LAYER_ARN=$(aws lambda list-layer-versions --layer-name "$AZURE_LAYER_NAME" \
      --region "$REGION" --query "LayerVersions[0].LayerVersionArn" --output text 2>/dev/null) \
      && [ -n "$AZURE_SDK_LAYER_ARN" ] && [ "$AZURE_SDK_LAYER_ARN" != "None" ]; then
    ok "Reusing existing layer: $AZURE_SDK_LAYER_ARN"
  else
    echo "  None found — building the Azure SDK layer now (this can take a few minutes)..."
    LAYER_BUILD_DIR=$(mktemp -d)
    # The layer zip regularly exceeds Lambda's ~50MB direct-upload limit for
    # publish-layer-version --zip-file, so it's staged via S3 instead, which
    # supports payloads up to the real 250MB unzipped layer limit.
    LAYER_STAGING_BUCKET="${RESOURCE_PREFIX}aviatrix-cleanup-layer-staging-${ACCOUNT_ID}"
    if pip3 install -r layer/azure-sdk/requirements.txt -t "$LAYER_BUILD_DIR/python" \
        --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12 >/dev/null 2>&1 \
      && python3 layer/azure-sdk/trim_unused_api_versions.py "$LAYER_BUILD_DIR/python" \
      && (cd "$LAYER_BUILD_DIR" && zip -rq azure-sdk-layer.zip python/) \
      && { aws s3api head-bucket --bucket "$LAYER_STAGING_BUCKET" --region "$REGION" >/dev/null 2>&1 \
           || aws s3api create-bucket --bucket "$LAYER_STAGING_BUCKET" --region "$REGION" \
                $( [ "$REGION" != "us-east-1" ] && echo "--create-bucket-configuration LocationConstraint=$REGION" ) >/dev/null 2>&1; } \
      && aws s3 cp "$LAYER_BUILD_DIR/azure-sdk-layer.zip" "s3://$LAYER_STAGING_BUCKET/azure-sdk-layer.zip" --region "$REGION" >/dev/null 2>&1 \
      && AZURE_SDK_LAYER_ARN=$(aws lambda publish-layer-version \
           --layer-name "$AZURE_LAYER_NAME" \
           --content "S3Bucket=$LAYER_STAGING_BUCKET,S3Key=azure-sdk-layer.zip" \
           --compatible-runtimes python3.12 \
           --region "$REGION" \
           --query LayerVersionArn --output text); then
      ok "Azure SDK layer published: $AZURE_SDK_LAYER_ARN"
      aws s3 rm "s3://$LAYER_STAGING_BUCKET/azure-sdk-layer.zip" --region "$REGION" >/dev/null 2>&1 || true
    else
      AZURE_SDK_LAYER_ARN=""
      warn "Automated layer build failed — see docs/AZURE_LAYER.md to build it manually and redeploy with AzureSdkLayerArn set. Continuing without Azure support for now."
    fi
    rm -rf "$LAYER_BUILD_DIR"
  fi
else
  ok "Skipping Azure — AzureSpSecretArn left blank"
fi

# ── 4. Optional GCP secret ───────────────────────────────────────────────────
step "GCP cleanup + instances support (optional)"

GCP_SA_SECRET_ARN=""
GCP_SECRET_NAME="${RESOURCE_PREFIX}aviatrix-cleanup/gcp-sa"
read -rp "Enable GCP support? [y/N] " enable_gcp
if [[ "$enable_gcp" =~ ^[Yy]$ ]]; then
  read -rp "  Already have a Secrets Manager ARN to reuse (e.g. a shared org secret)? Leave blank to create/update $GCP_SECRET_NAME: " GCP_SA_SECRET_ARN

  if [ -z "$GCP_SA_SECRET_ARN" ]; then
    read -rp "  Path to your GCP service account key JSON file: " GCP_KEY_PATH
    if [ ! -f "$GCP_KEY_PATH" ]; then
      err "File not found: $GCP_KEY_PATH"
      exit 1
    fi
    if GCP_SA_SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "$GCP_SECRET_NAME" --query ARN --output text 2>/dev/null); then
      warn "Secret $GCP_SECRET_NAME already exists — updating it."
      aws secretsmanager put-secret-value --secret-id "$GCP_SECRET_NAME" --secret-string "file://$GCP_KEY_PATH" >/dev/null
    else
      GCP_SA_SECRET_ARN=$(aws secretsmanager create-secret \
        --name "$GCP_SECRET_NAME" \
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

OVERRIDES="StageName=prod ResourcePrefix=\"$RESOURCE_PREFIX\" LoginPassword=\"$LOGIN_PASSWORD\" AuthSigningKey=\"$AUTH_SIGNING_KEY\""
[ -n "$AZURE_SP_SECRET_ARN" ] && OVERRIDES="$OVERRIDES AzureSpSecretArn=\"$AZURE_SP_SECRET_ARN\""
[ -n "$AZURE_SDK_LAYER_ARN" ] && OVERRIDES="$OVERRIDES AzureSdkLayerArn=\"$AZURE_SDK_LAYER_ARN\""
[ -n "$GCP_SA_SECRET_ARN" ]   && OVERRIDES="$OVERRIDES GcpSaSecretArn=\"$GCP_SA_SECRET_ARN\""

if $FIRST_DEPLOY; then
  # First deploy for this stack: run the full wizard once so stack
  # name/region/capabilities get saved to its own config file for every
  # future run. Stack name and config file are pre-filled — press Enter
  # through the wizard's prompts to accept them.
  ok "Guided deploy — stack name and config file are pre-filled, press Enter to accept."
  eval sam deploy --guided --stack-name "$STACK_NAME" --config-file "$CONFIG_FILE" --region "$REGION" --parameter-overrides "$OVERRIDES"
else
  # Re-run: reuse this stack's saved config file, only override parameters.
  eval sam deploy --stack-name "$STACK_NAME" --config-file "$CONFIG_FILE" --region "$REGION" --parameter-overrides "$OVERRIDES"
fi

if ! aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
  err "Stack '$STACK_NAME' not found after deploy — did you type a different name in the guided wizard? Re-run and accept the pre-filled stack name."
  exit 1
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
