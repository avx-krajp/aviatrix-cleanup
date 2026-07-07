#!/usr/bin/env bash
# =============================================================================
# azure_cleanup.sh — Azure VNet/resource-group cleanup logic
# Sourced by aviatrix_cleanup.sh; expects these variables already set:
#   REGION, RESOURCE_GROUP, VNET_NAME (optional), SUBSCRIPTION_ID (optional),
#   DRY_RUN, FORCE
# Exports: run_azure_cleanup
# =============================================================================

# ── Azure CLI helper ──────────────────────────────────────────────────────────
# All az calls go through this so --subscription is injected consistently
az_() {
  if [ -n "$SUBSCRIPTION_ID" ]; then
    az --subscription "$SUBSCRIPTION_ID" "$@"
  else
    az "$@"
  fi
}

# ── Resolve resource group list ───────────────────────────────────────────────
resolve_azure_rgs() {
  if [ -n "$RESOURCE_GROUP" ]; then
    RG_LIST="$RESOURCE_GROUP"
    return
  fi

  printf "${C}No --resource-group specified — fetching all resource groups in %s...${N}\n" "$REGION"
  local list
  list=$(az_ group list \
    --query "[?location=='$REGION'].[name,tags.aviatrix]" \
    --output tsv 2>&1) || { printf "${R}ERROR:${N} %s\n" "$list" >&2; exit 1; }

  RG_LIST=""
  while IFS=$'\t' read -r _rg _tag; do
    [ -z "$_rg" ] && continue
    RG_LIST="$RG_LIST $_rg"
    printf "  ${C}→${N} %-40s  (%s)\n" "$_rg" "${_tag:-<no aviatrix tag>}"
  done <<EOF
$list
EOF
  RG_LIST=$(printf '%s' "$RG_LIST" | tr -s ' ' | sed 's/^ //')

  if [ -z "$RG_LIST" ]; then
    printf "${Y}No resource groups found in region %s.${N}\n" "$REGION"
    exit 0
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# Per-resource-group cleanup
# ══════════════════════════════════════════════════════════════════════════════
run_rg_cleanup() {
  # $RG = current resource group (set by caller)
  # $VNET_NAME = optional VNet scope (empty = all VNets in RG)

  # Collect subnet IDs up front — used by multiple steps to scope resources
  if [ -n "$VNET_NAME" ]; then
    AZURE_SUBNET_IDS=$(az_ network vnet subnet list \
      --resource-group "$RG" --vnet-name "$VNET_NAME" \
      --query '[].id' --output tsv 2>/dev/null | tr '\t' '\n') || AZURE_SUBNET_IDS=""
  else
    AZURE_SUBNET_IDS=$(az_ network vnet subnet list \
      --resource-group "$RG" \
      --query '[].id' --output tsv 2>/dev/null | tr '\t' '\n') || AZURE_SUBNET_IDS=""
  fi

# ──────────────────────────────────────────────────────────────────────────────
step "1/22" "Resource Locks"
LOCK_DATA=$(az_ lock list --resource-group "$RG" \
  --query '[].[name,level,id]' --output tsv 2>/dev/null) || LOCK_DATA=""

if [ -z "$LOCK_DATA" ] || printf '%s' "$LOCK_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _lname _llevel _lid; do
    [ -z "$_lname" ] && continue
    found_msg "$_lname  level=$_llevel"
    run_delete "Lock $_lname" \
      az lock delete --ids "$_lid"
  done <<EOF
$LOCK_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "2/22" "AKS Clusters"
AKS_DATA=$(az_ aks list --resource-group "$RG" \
  --query '[].[name,provisioningState,kubernetesVersion]' --output tsv 2>/dev/null) || AKS_DATA=""

if [ -z "$AKS_DATA" ] || printf '%s' "$AKS_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _aname _astate _aver; do
    [ -z "$_aname" ] && continue
    [ "$_astate" = "Deleting" ] && skip_msg "$_aname already deleting" && continue
    found_msg "$_aname  state=$_astate  k8s=$_aver"

    # Delete agent pools (node pools) first
    NP_DATA=$(az_ aks nodepool list --resource-group "$RG" --cluster-name "$_aname" \
      --query '[].[name,provisioningState]' --output tsv 2>/dev/null) || NP_DATA=""
    while IFS=$'\t' read -r _npname _npstate; do
      [ -z "$_npname" ] && continue
      [ "$_npname" = "System" ] && continue  # system pool deleted with cluster
      found_msg "  node pool: $_npname  state=$_npstate"
      run_delete "AKS node pool $_npname in $_aname" \
        az aks nodepool delete --resource-group "$RG" \
          --cluster-name "$_aname" --name "$_npname" --no-wait
    done <<EOF
$NP_DATA
EOF
    run_delete "AKS cluster $_aname" \
      az aks delete --resource-group "$RG" --name "$_aname" --yes --no-wait
  done <<EOF
$AKS_DATA
EOF
  if ! $DRY_RUN; then
    info_msg "waiting up to 15 min for AKS clusters to delete..."
    az_ aks wait --resource-group "$RG" --name "$(printf '%s' "$AKS_DATA" | awk 'NR==1{print $1}')" \
      --deleted 2>/dev/null || true
    sleep 30
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "3/22" "VM Scale Sets (VMSS)"
VMSS_DATA=$(az_ vmss list --resource-group "$RG" \
  --query '[].[name,provisioningState,sku.capacity]' --output tsv 2>/dev/null) || VMSS_DATA=""

if [ -z "$VMSS_DATA" ] || printf '%s' "$VMSS_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _vname _vstate _vcap; do
    [ -z "$_vname" ] && continue
    [ "$_vstate" = "Deleting" ] && skip_msg "$_vname already deleting" && continue
    found_msg "$_vname  state=$_vstate  instances=$_vcap"
    run_delete "VMSS $_vname" \
      az vmss delete --resource-group "$RG" --name "$_vname" --force-deletion true
  done <<EOF
$VMSS_DATA
EOF
  if ! $DRY_RUN; then
    info_msg "waiting 30s for VMSS deletions..."
    sleep 30
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "4/22" "Virtual Machines"
VM_DATA=$(az_ vm list --resource-group "$RG" \
  --query '[].[name,powerState,hardwareProfile.vmSize]' --output tsv 2>/dev/null) || VM_DATA=""

if [ -z "$VM_DATA" ] || printf '%s' "$VM_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _vname _vstate _vsize; do
    [ -z "$_vname" ] && continue
    found_msg "$_vname  state=${_vstate:-unknown}  size=$_vsize"
    run_delete "VM $_vname" \
      az vm delete --resource-group "$RG" --name "$_vname" --yes --force-deletion true
  done <<EOF
$VM_DATA
EOF
  if ! $DRY_RUN; then
    info_msg "waiting 20s for VM deletions..."
    sleep 20
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "5/22" "Managed Disks (unattached)"
DISK_DATA=$(az_ disk list --resource-group "$RG" \
  --query '[?diskState==`Unattached`].[name,diskSizeGb,timeCreated]' \
  --output tsv 2>/dev/null) || DISK_DATA=""

if [ -z "$DISK_DATA" ] || printf '%s' "$DISK_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _dname _dsz _dtime; do
    [ -z "$_dname" ] && continue
    found_msg "$_dname  ${_dsz}GiB  created=$_dtime"
    run_delete "Managed Disk $_dname" \
      az disk delete --resource-group "$RG" --name "$_dname" --yes
  done <<EOF
$DISK_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "6/22" "NAT Gateways"
NAT_DATA=$(az_ network nat gateway list --resource-group "$RG" \
  --query '[].[name,provisioningState]' --output tsv 2>/dev/null) || NAT_DATA=""

if [ -z "$NAT_DATA" ] || printf '%s' "$NAT_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _nname _nstate; do
    [ -z "$_nname" ] && continue
    found_msg "$_nname  state=$_nstate"
    run_delete "NAT Gateway $_nname" \
      az network nat gateway delete --resource-group "$RG" --name "$_nname"
  done <<EOF
$NAT_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "7/22" "Azure SQL Servers + Databases"
SQL_DATA=$(az_ sql server list --resource-group "$RG" \
  --query '[].[name,state,fullyQualifiedDomainName]' --output tsv 2>/dev/null) || SQL_DATA=""

if [ -z "$SQL_DATA" ] || printf '%s' "$SQL_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _sname _sstate _sfqdn; do
    [ -z "$_sname" ] && continue
    found_msg "server: $_sname  state=$_sstate"
    # Delete databases first (except master)
    DB_DATA=$(az_ sql db list --resource-group "$RG" --server "$_sname" \
      --query '[?name!=`master`].name' --output tsv 2>/dev/null) || DB_DATA=""
    for _db in $DB_DATA; do
      [ -z "$_db" ] && continue
      found_msg "  db: $_db"
      run_delete "SQL DB $_db on $_sname" \
        az sql db delete --resource-group "$RG" --server "$_sname" --name "$_db" --yes
    done
    run_delete "SQL Server $_sname" \
      az sql server delete --resource-group "$RG" --name "$_sname" --yes
  done <<EOF
$SQL_DATA
EOF
fi

# Flexible servers (PostgreSQL / MySQL)
for _engine in postgres mysql; do
  FS_DATA=$(az_ "$_engine" flexible-server list --resource-group "$RG" \
    --query '[].[name,state,sku.name]' --output tsv 2>/dev/null) || FS_DATA=""
  [ -z "$FS_DATA" ] || printf '%s' "$FS_DATA" | grep -q "^$" && continue
  while IFS=$'\t' read -r _fsname _fsstate _fssku; do
    [ -z "$_fsname" ] && continue
    found_msg "$_engine flexible-server: $_fsname  state=$_fsstate  sku=$_fssku"
    run_delete "$_engine flexible-server $_fsname" \
      az "$_engine" flexible-server delete --resource-group "$RG" --name "$_fsname" --yes
  done <<EOF
$FS_DATA
EOF
done

# ──────────────────────────────────────────────────────────────────────────────
step "8/22" "Azure Cache for Redis"
REDIS_DATA=$(az_ redis list --resource-group "$RG" \
  --query '[].[name,provisioningState,sku.name,sku.capacity]' --output tsv 2>/dev/null) || REDIS_DATA=""

if [ -z "$REDIS_DATA" ] || printf '%s' "$REDIS_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _rname _rstate _rsku _rcap; do
    [ -z "$_rname" ] && continue
    [ "$_rstate" = "Deleting" ] && skip_msg "$_rname already deleting" && continue
    found_msg "$_rname  state=$_rstate  sku=$_rsku  capacity=$_rcap"
    run_delete "Redis cache $_rname" \
      az redis delete --resource-group "$RG" --name "$_rname" --yes
  done <<EOF
$REDIS_DATA
EOF
  if ! $DRY_RUN; then
    info_msg "waiting 30s for Redis deletion (NICs need to release)..."
    sleep 30
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "9/22" "Azure Cognitive Search"
SEARCH_DATA=$(az_ search service list --resource-group "$RG" \
  --query '[].[name,provisioningState,sku.name]' --output tsv 2>/dev/null) || SEARCH_DATA=""

if [ -z "$SEARCH_DATA" ] || printf '%s' "$SEARCH_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _sname _sstate _ssku; do
    [ -z "$_sname" ] && continue
    found_msg "$_sname  state=$_sstate  sku=$_ssku"
    run_delete "Search service $_sname" \
      az search service delete --resource-group "$RG" --name "$_sname" --yes
  done <<EOF
$SEARCH_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "10/22" "Azure Event Hubs Namespaces (Kafka)"
EH_DATA=$(az_ eventhubs namespace list --resource-group "$RG" \
  --query '[].[name,provisioningState,sku.name,isAutoInflateEnabled]' \
  --output tsv 2>/dev/null) || EH_DATA=""

if [ -z "$EH_DATA" ] || printf '%s' "$EH_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _ename _estate _esku _eai; do
    [ -z "$_ename" ] && continue
    found_msg "$_ename  state=$_estate  sku=$_esku"
    run_delete "Event Hubs namespace $_ename" \
      az eventhubs namespace delete --resource-group "$RG" --name "$_ename"
  done <<EOF
$EH_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "11/22" "Azure Synapse Workspaces"
SYN_DATA=$(az_ synapse workspace list --resource-group "$RG" \
  --query '[].[name,provisioningState]' --output tsv 2>/dev/null) || SYN_DATA=""

if [ -z "$SYN_DATA" ] || printf '%s' "$SYN_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _wname _wstate; do
    [ -z "$_wname" ] && continue
    found_msg "$_wname  state=$_wstate"
    run_delete "Synapse workspace $_wname" \
      az synapse workspace delete --resource-group "$RG" --name "$_wname" --yes
  done <<EOF
$SYN_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "12/22" "Network Interfaces (detached)"
NIC_DATA=$(az_ network nic list --resource-group "$RG" \
  --query '[?virtualMachine==null].[name,provisioningState]' \
  --output tsv 2>/dev/null) || NIC_DATA=""

if [ -z "$NIC_DATA" ] || printf '%s' "$NIC_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _nname _nstate; do
    [ -z "$_nname" ] && continue
    found_msg "$_nname  state=$_nstate"
    run_delete "NIC $_nname" \
      az network nic delete --resource-group "$RG" --name "$_nname"
  done <<EOF
$NIC_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "13/22" "Public IPs (unassociated)"
PIP_DATA=$(az_ network public-ip list --resource-group "$RG" \
  --query '[?ipConfiguration==null].[name,publicIpAllocationMethod,ipAddress]' \
  --output tsv 2>/dev/null) || PIP_DATA=""

if [ -z "$PIP_DATA" ] || printf '%s' "$PIP_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _pname _palloc _pip; do
    [ -z "$_pname" ] && continue
    found_msg "$_pname  alloc=$_palloc  ip=${_pip:-<unallocated>}"
    run_delete "Public IP $_pname" \
      az network public-ip delete --resource-group "$RG" --name "$_pname"
  done <<EOF
$PIP_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "14/22" "Load Balancers + Application Gateways"
LB_DATA=$(az_ network lb list --resource-group "$RG" \
  --query '[].[name,provisioningState,sku.name]' --output tsv 2>/dev/null) || LB_DATA=""

if [ -z "$LB_DATA" ] || printf '%s' "$LB_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _lname _lstate _lsku; do
    [ -z "$_lname" ] && continue
    found_msg "$_lname  state=$_lstate  sku=$_lsku"
    run_delete "Load Balancer $_lname" \
      az network lb delete --resource-group "$RG" --name "$_lname"
  done <<EOF
$LB_DATA
EOF
fi

AGW_DATA=$(az_ network application-gateway list --resource-group "$RG" \
  --query '[].[name,provisioningState,sku.name]' --output tsv 2>/dev/null) || AGW_DATA=""
if [ -n "$AGW_DATA" ] && ! printf '%s' "$AGW_DATA" | grep -q "^$"; then
  while IFS=$'\t' read -r _agname _agstate _agsku; do
    [ -z "$_agname" ] && continue
    found_msg "App Gateway: $_agname  state=$_agstate  sku=$_agsku"
    run_delete "Application Gateway $_agname" \
      az network application-gateway delete --resource-group "$RG" --name "$_agname"
  done <<EOF
$AGW_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "15/22" "Private Endpoints"
PE_DATA=$(az_ network private-endpoint list --resource-group "$RG" \
  --query '[].[name,provisioningState]' --output tsv 2>/dev/null) || PE_DATA=""

if [ -z "$PE_DATA" ] || printf '%s' "$PE_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _pname _pstate; do
    [ -z "$_pname" ] && continue
    found_msg "$_pname  state=$_pstate"
    run_delete "Private Endpoint $_pname" \
      az network private-endpoint delete --resource-group "$RG" --name "$_pname"
  done <<EOF
$PE_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "16/22" "VNet Peerings"
if [ -n "$VNET_NAME" ]; then
  _vnets_to_check="$VNET_NAME"
else
  _vnets_to_check=$(az_ network vnet list --resource-group "$RG" \
    --query '[].name' --output tsv 2>/dev/null | tr '\t' '\n') || _vnets_to_check=""
fi

_found_any_peer=false
while IFS= read -r _vnet; do
  [ -z "$_vnet" ] && continue
  PEER_DATA=$(az_ network vnet peering list --resource-group "$RG" --vnet-name "$_vnet" \
    --query '[].[name,peeringState]' --output tsv 2>/dev/null) || PEER_DATA=""
  [ -z "$PEER_DATA" ] && continue
  while IFS=$'\t' read -r _pname _pstate; do
    [ -z "$_pname" ] && continue
    found_msg "$_pname  (vnet=$_vnet)  state=$_pstate"
    run_delete "VNet peering $_pname in $_vnet" \
      az network vnet peering delete --resource-group "$RG" --vnet-name "$_vnet" --name "$_pname"
    _found_any_peer=true
  done <<EOF2
$PEER_DATA
EOF2
done <<EOF
$_vnets_to_check
EOF
$_found_any_peer || none_msg

# ──────────────────────────────────────────────────────────────────────────────
step "17/22" "VPN Gateway + Connections"
VGW_DATA=$(az_ network vnet-gateway list --resource-group "$RG" \
  --query '[].[name,provisioningState,gatewayType,vpnType]' --output tsv 2>/dev/null) || VGW_DATA=""

if [ -z "$VGW_DATA" ] || printf '%s' "$VGW_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _gname _gstate _gtype _gvpn; do
    [ -z "$_gname" ] && continue
    found_msg "gateway: $_gname  state=$_gstate  type=$_gtype"
    # Delete connections first
    CONN_DATA=$(az_ network vpn-connection list --resource-group "$RG" \
      --query "[?virtualNetworkGateway1.id contains '$_gname'].[name,connectionStatus]" \
      --output tsv 2>/dev/null) || CONN_DATA=""
    while IFS=$'\t' read -r _cname _cstatus; do
      [ -z "$_cname" ] && continue
      found_msg "  connection: $_cname  status=$_cstatus"
      run_delete "VPN connection $_cname" \
        az network vpn-connection delete --resource-group "$RG" --name "$_cname"
    done <<EOF2
$CONN_DATA
EOF2
    run_delete "VPN Gateway $_gname" \
      az network vnet-gateway delete --resource-group "$RG" --name "$_gname"
  done <<EOF
$VGW_DATA
EOF
  if ! $DRY_RUN; then
    info_msg "waiting 30s for gateway deletion..."
    sleep 30
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "18/22" "Network Security Groups"
NSG_DATA=$(az_ network nsg list --resource-group "$RG" \
  --query '[].[name,provisioningState]' --output tsv 2>/dev/null) || NSG_DATA=""

if [ -z "$NSG_DATA" ] || printf '%s' "$NSG_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _nname _nstate; do
    [ -z "$_nname" ] && continue
    found_msg "$_nname  state=$_nstate"
    run_delete "NSG $_nname" \
      az network nsg delete --resource-group "$RG" --name "$_nname"
  done <<EOF
$NSG_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "19/22" "Route Tables (UDRs)"
RT_DATA=$(az_ network route-table list --resource-group "$RG" \
  --query '[].[name,provisioningState]' --output tsv 2>/dev/null) || RT_DATA=""

if [ -z "$RT_DATA" ] || printf '%s' "$RT_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _rname _rstate; do
    [ -z "$_rname" ] && continue
    found_msg "$_rname  state=$_rstate"
    run_delete "Route Table $_rname" \
      az network route-table delete --resource-group "$RG" --name "$_rname"
  done <<EOF
$RT_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "20/22" "Subnets"
while IFS= read -r _vnet; do
  [ -z "$_vnet" ] && continue
  SN_DATA=$(az_ network vnet subnet list --resource-group "$RG" --vnet-name "$_vnet" \
    --query '[].[name,addressPrefix,provisioningState]' --output tsv 2>/dev/null) || SN_DATA=""
  [ -z "$SN_DATA" ] && continue
  while IFS=$'\t' read -r _sname _scidr _sstate; do
    [ -z "$_sname" ] && continue
    found_msg "$_sname  $_scidr  (vnet=$_vnet)  state=$_sstate"
    run_delete "Subnet $_sname in $_vnet" \
      az network vnet subnet delete --resource-group "$RG" --vnet-name "$_vnet" --name "$_sname"
  done <<EOF2
$SN_DATA
EOF2
done <<EOF
$_vnets_to_check
EOF

# ──────────────────────────────────────────────────────────────────────────────
step "21/22" "Virtual Networks"
while IFS= read -r _vnet; do
  [ -z "$_vnet" ] && continue
  _vstate=$(az_ network vnet show --resource-group "$RG" --name "$_vnet" \
    --query 'provisioningState' --output tsv 2>/dev/null) || _vstate="unknown"
  found_msg "$_vnet  state=$_vstate"
  run_delete "VNet $_vnet" \
    az network vnet delete --resource-group "$RG" --name "$_vnet"
done <<EOF
$_vnets_to_check
EOF

# ──────────────────────────────────────────────────────────────────────────────
step "22/22" "Resource Group: $RG"
if $DRY_RUN; then
  printf "  ${Y}[dry-run]${N} would delete resource group %s\n" "$RG"
else
  found_msg "$RG"
  ERR=$(az_ group delete --name "$RG" --yes --no-wait 2>&1)
  if [ $? -eq 0 ]; then
    ok_msg "Resource group $RG deletion initiated (async)"
  else
    err_msg "RG deletion failed: $(printf '%s' "$ERR" | tail -1)"
    printf "\n${Y}${B}  ─── Remaining resources in %s ───${N}\n" "$RG"
    _remaining=$(az_ resource list --resource-group "$RG" \
      --query '[].[type,name]' --output tsv 2>/dev/null | head -20) || _remaining=""
    if [ -n "$_remaining" ]; then
      while IFS=$'\t' read -r _rtype _rname; do
        printf "  ${R}REMAINS${N} %-45s %s\n" "$_rtype" "$_rname"
      done <<EOF2
$_remaining
EOF2
    fi
    printf "\n  ${C}Re-run this script to retry, or resolve the above manually.${N}\n"
  fi
fi

} # end run_rg_cleanup

# ══════════════════════════════════════════════════════════════════════════════
# run_azure_cleanup — entry point called by dispatcher
# ══════════════════════════════════════════════════════════════════════════════
run_azure_cleanup() {
  # Verify az CLI is available and logged in
  if ! command -v az > /dev/null 2>&1; then
    printf "${R}ERROR:${N} Azure CLI (az) not found. Install from https://aka.ms/installazureclimacos\n" >&2
    exit 1
  fi
  if ! az account show > /dev/null 2>&1; then
    printf "${R}ERROR:${N} Not logged in to Azure. Run: az login\n" >&2
    exit 1
  fi

  if [ -n "$SUBSCRIPTION_ID" ]; then
    az account set --subscription "$SUBSCRIPTION_ID" 2>/dev/null || true
  fi

  resolve_azure_rgs

  local _rg_count
  _rg_count=$(printf '%s' "$RG_LIST" | wc -w | tr -d ' ')
  local _scope="$_rg_count resource group(s) in $REGION"
  [ -n "$VNET_NAME" ] && _scope="VNet $VNET_NAME in $RESOURCE_GROUP"
  print_banner "azure" "$REGION" "$_scope"
  confirm_or_abort "Resources in $_scope"

  # Post-RG accumulators
  ALL_SNAPSHOTS=""
  ALL_IMAGES=""

  local _rg_idx=0
  for RG in $RG_LIST; do
    _rg_idx=$(( _rg_idx + 1 ))
    if [ "$_rg_count" -gt 1 ]; then
      printf "\n${B}╔══════════════════════════════════════════════════════════╗${N}\n"
      printf "${B}║  RG %d of %d: %-44s║${N}\n" "$_rg_idx" "$_rg_count" "$RG "
      printf "${B}╚══════════════════════════════════════════════════════════╝${N}\n"
    fi
    run_rg_cleanup
  done

  # ── Post-RG region-level steps ──────────────────────────────────────────────
  step "+A" "Snapshots (self-owned)"
  SNAP_DATA=$(az_ snapshot list \
    --query "[?location=='$REGION'].[name,resourceGroup,diskSizeGb,timeCreated]" \
    --output tsv 2>/dev/null) || SNAP_DATA=""
  if [ -z "$SNAP_DATA" ] || printf '%s' "$SNAP_DATA" | grep -q "^$"; then
    none_msg
  else
    while IFS=$'\t' read -r _sname _srg _ssz _stime; do
      [ -z "$_sname" ] && continue
      found_msg "$_sname  rg=$_srg  ${_ssz}GiB  created=$_stime"
      run_delete "Snapshot $_sname" \
        az snapshot delete --resource-group "$_srg" --name "$_sname" --yes
    done <<EOF
$SNAP_DATA
EOF
  fi

  step "+B" "Custom Images / Shared Image Gallery"
  IMG_DATA=$(az_ image list \
    --query "[?location=='$REGION'].[name,resourceGroup,hyperVGeneration]" \
    --output tsv 2>/dev/null) || IMG_DATA=""
  if [ -z "$IMG_DATA" ] || printf '%s' "$IMG_DATA" | grep -q "^$"; then
    none_msg
  else
    while IFS=$'\t' read -r _iname _irg _ihv; do
      [ -z "$_iname" ] && continue
      found_msg "$_iname  rg=$_irg  hyper-v=$_ihv"
      run_delete "Image $_iname" \
        az image delete --resource-group "$_irg" --name "$_iname"
    done <<EOF
$IMG_DATA
EOF
  fi

  # Shared Image Gallery definitions + versions
  SIG_DATA=$(az_ sig list \
    --query "[?location=='$REGION'].[name,resourceGroup]" \
    --output tsv 2>/dev/null) || SIG_DATA=""
  if [ -n "$SIG_DATA" ] && ! printf '%s' "$SIG_DATA" | grep -q "^$"; then
    while IFS=$'\t' read -r _gname _grg; do
      [ -z "$_gname" ] && continue
      found_msg "Shared Image Gallery: $_gname  rg=$_grg"
      # Delete image definitions (versions cascade)
      DEF_DATA=$(az_ sig image-definition list --resource-group "$_grg" --gallery-name "$_gname" \
        --query '[].name' --output tsv 2>/dev/null) || DEF_DATA=""
      for _def in $DEF_DATA; do
        found_msg "  image definition: $_def"
        VER_DATA=$(az_ sig image-version list --resource-group "$_grg" --gallery-name "$_gname" \
          --gallery-image-definition "$_def" --query '[].name' --output tsv 2>/dev/null) || VER_DATA=""
        for _ver in $VER_DATA; do
          run_delete "Image version $_ver in $_def" \
            az sig image-version delete --resource-group "$_grg" --gallery-name "$_gname" \
              --gallery-image-definition "$_def" --gallery-image-version "$_ver"
        done
        run_delete "Image definition $_def" \
          az sig image-definition delete --resource-group "$_grg" --gallery-name "$_gname" \
            --gallery-image-definition "$_def"
      done
      run_delete "Shared Image Gallery $_gname" \
        az sig delete --resource-group "$_grg" --gallery-name "$_gname"
    done <<EOF
$SIG_DATA
EOF
  fi

  step "+C" "Container Registries (ACR)"
  ACR_DATA=$(az_ acr list \
    --query "[?location=='$REGION'].[name,resourceGroup,sku.name]" \
    --output tsv 2>/dev/null) || ACR_DATA=""
  if [ -z "$ACR_DATA" ] || printf '%s' "$ACR_DATA" | grep -q "^$"; then
    none_msg
  else
    while IFS=$'\t' read -r _aname _arg _asku; do
      [ -z "$_aname" ] && continue
      found_msg "$_aname  rg=$_arg  sku=$_asku"
      run_delete "ACR $_aname" \
        az acr delete --resource-group "$_arg" --name "$_aname" --yes
    done <<EOF
$ACR_DATA
EOF
  fi

  step "+D" "SSH Public Keys"
  KEY_DATA=$(az_ sshkey list \
    --query "[?location=='$REGION'].[name,resourceGroup]" \
    --output tsv 2>/dev/null) || KEY_DATA=""
  if [ -z "$KEY_DATA" ] || printf '%s' "$KEY_DATA" | grep -q "^$"; then
    none_msg
  else
    while IFS=$'\t' read -r _kname _krg; do
      [ -z "$_kname" ] && continue
      found_msg "$_kname  rg=$_krg"
      run_delete "SSH Key $_kname" \
        az sshkey delete --resource-group "$_krg" --name "$_kname" --yes
    done <<EOF
$KEY_DATA
EOF
  fi
}
