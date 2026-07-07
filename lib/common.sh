#!/usr/bin/env bash
# =============================================================================
# common.sh — shared helpers sourced by aws_cleanup.sh and azure_cleanup.sh
# =============================================================================

# ── Colours ───────────────────────────────────────────────────────────────────
R='\033[0;31m'; Y='\033[1;33m'; G='\033[0;32m'
C='\033[0;36m'; B='\033[1m'; N='\033[0m'

# ── Output helpers ────────────────────────────────────────────────────────────
step()       { printf "\n${B}━━━ [%s] %s ━━━${N}\n" "$1" "$2"; }
found_msg()  { printf "  ${Y}→ found:${N}  %s\n" "$*"; }
delete_msg() { printf "  ${R}✗ delete:${N} %s\n" "$*"; }
skip_msg()   { printf "  ${G}✓ skip:${N}   %s\n" "$*"; }
ok_msg()     { printf "  ${G}✓ done:${N}   %s\n" "$*"; }
err_msg()    { printf "  ${R}✗ error:${N}  %s\n" "$*"; }
info_msg()   { printf "  ${C}ℹ info:${N}   %s\n" "$*"; }
none_msg()   { printf "  (none)\n"; }

# run_delete LABEL cmd arg...
# Prints label, runs the command, reports success or error.
# In dry-run mode prints what would run without executing.
run_delete() {
  local label="$1"; shift
  if $DRY_RUN; then
    printf "  ${Y}[dry-run]${N} would run: %s\n" "$*"
    return 0
  fi
  delete_msg "$label"
  local out
  out=$("$@" 2>&1) && ok_msg "$label" || \
    err_msg "$label — $(printf '%s' "$out" | tail -1)"
}

# print_banner CLOUD REGION SCOPE_LABEL MODE
print_banner() {
  local cloud="$1" region="$2" scope="$3"
  printf "\n${B}╔══════════════════════════════════════════════════════════╗${N}\n"
  printf "${B}║   Aviatrix Cleanup  |  %-5s  |  Region: %-14s║${N}\n" \
    "$(printf '%s' "$cloud" | tr '[:lower:]' '[:upper:]')" "$region"
  printf "${B}╚══════════════════════════════════════════════════════════╝${N}\n"
  printf "  Scope   : %s\n" "$scope"
  $DRY_RUN && printf "  Mode    : DRY-RUN (nothing will be deleted)\n" \
           || printf "  Mode    : LIVE DELETE\n"
  printf "\n"
}

# confirm_or_abort DESCRIPTION
confirm_or_abort() {
  local desc="$1"
  if ! $DRY_RUN && ! $FORCE; then
    printf "${Y}%s will be PERMANENTLY DELETED.${N}\n" "$desc"
    printf "Type 'yes' to continue: "
    read -r _confirm
    [ "$_confirm" = "yes" ] || { printf "Aborted.\n"; exit 0; }
  fi
}
