#!/usr/bin/env bash
# =============================================================================
# Aviatrix Cloud Cleanup — AWS + Azure
#
# Usage (AWS):
#   ./aviatrix_cleanup.sh --cloud aws --region <region> [--vpc-id <vpc-id>] \
#                         [--dry-run] [--force]
#
# Usage (Azure):
#   ./aviatrix_cleanup.sh --cloud azure --region <location> \
#                         --resource-group <rg> [--vnet <vnet-name>] \
#                         [--subscription-id <sub-id>] \
#                         [--dry-run] [--force]
#
# When --vpc-id / --resource-group is omitted, ALL resources in the given
# region are cleaned up.
#
# Flags:
#   --cloud            aws | azure  (required)
#   --region           AWS region or Azure location  (required)
#   --vpc-id           AWS: scope to a single VPC
#   --resource-group   Azure: scope to a single resource group
#   --vnet             Azure: scope to a single VNet within the resource group
#   --subscription-id  Azure: override active subscription
#   --dry-run          Print what would be deleted without deleting anything
#   --force            Skip confirmation prompt
# =============================================================================
set +H 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Source shared helpers ─────────────────────────────────────────────────────
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

# ── Parse args ────────────────────────────────────────────────────────────────
CLOUD=""
REGION=""
VPC_ID=""
RESOURCE_GROUP=""
VNET_NAME=""
SUBSCRIPTION_ID=""
DRY_RUN=false
FORCE=false

while [ $# -gt 0 ]; do
  case "$1" in
    --cloud)            CLOUD="$2";            shift 2 ;;
    --region)           REGION="$2";           shift 2 ;;
    --vpc-id)           VPC_ID="$2";           shift 2 ;;
    --resource-group)   RESOURCE_GROUP="$2";   shift 2 ;;
    --vnet)             VNET_NAME="$2";        shift 2 ;;
    --subscription-id)  SUBSCRIPTION_ID="$2";  shift 2 ;;
    --dry-run)          DRY_RUN=true;          shift   ;;
    --force)            FORCE=true;            shift   ;;
    *)
      printf "Unknown argument: %s\n" "$1" >&2
      printf "Usage: %s --cloud aws|azure --region <region> [options]\n" "$0" >&2
      exit 1
      ;;
  esac
done

# ── Validate ──────────────────────────────────────────────────────────────────
if [ -z "$CLOUD" ]; then
  printf "${R}ERROR:${N} --cloud is required (aws or azure)\n" >&2
  exit 1
fi

if [ -z "$REGION" ]; then
  # AWS fallback: read from env / CLI config
  if [ "$CLOUD" = "aws" ]; then
    REGION="${AWS_DEFAULT_REGION:-}"
    [ -z "$REGION" ] && REGION=$(aws configure get region 2>/dev/null) || REGION=""
  fi
  if [ -z "$REGION" ]; then
    printf "${R}ERROR:${N} --region is required\n" >&2
    exit 1
  fi
fi

# ── Dispatch ──────────────────────────────────────────────────────────────────
case "$CLOUD" in
  aws)
    # shellcheck source=lib/aws_cleanup.sh
    . "$SCRIPT_DIR/lib/aws_cleanup.sh"
    run_aws_cleanup
    ;;
  azure)
    # shellcheck source=lib/azure_cleanup.sh
    . "$SCRIPT_DIR/lib/azure_cleanup.sh"
    run_azure_cleanup
    ;;
  *)
    printf "${R}ERROR:${N} Unknown cloud '%s'. Use 'aws' or 'azure'.\n" "$CLOUD" >&2
    exit 1
    ;;
esac

# ── Done ──────────────────────────────────────────────────────────────────────
printf "\n${G}${B}╔═══════════════════════════════════════════╗${N}\n"
$DRY_RUN && \
  printf "${G}${B}║  DRY-RUN complete — no resources deleted.  ║${N}\n" || \
  printf "${G}${B}║  Cleanup complete for %-20s║${N}\n" "$REGION."
printf "${G}${B}╚═══════════════════════════════════════════╝${N}\n\n"
