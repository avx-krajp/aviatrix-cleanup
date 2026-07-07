#!/usr/bin/env bash
# =============================================================================
# aws_cleanup.sh — AWS VPC cleanup logic
# Sourced by aviatrix_cleanup.sh; expects these variables already set:
#   REGION, VPC_ID (optional), DRY_RUN, FORCE
# Exports: run_aws_cleanup
# =============================================================================

# ── AWS CLI helpers ───────────────────────────────────────────────────────────
ec2()       { aws --region "$REGION" ec2            "$@"; }
elb()       { aws --region "$REGION" elbv2           "$@"; }
elb1()      { aws --region "$REGION" elb             "$@"; }
asg()       { aws --region "$REGION" autoscaling     "$@"; }
rds()       { aws --region "$REGION" rds             "$@"; }
ecache()    { aws --region "$REGION" elasticache     "$@"; }
efs()       { aws --region "$REGION" efs             "$@"; }
eks()       { aws --region "$REGION" eks             "$@"; }
opensearch(){ aws --region "$REGION" opensearch      "$@"; }
kafka()     { aws --region "$REGION" kafka           "$@"; }
redshift()  { aws --region "$REGION" redshift        "$@"; }
ecr()       { aws --region "$REGION" ecr             "$@"; }

# ── Resolve VPC list ──────────────────────────────────────────────────────────
resolve_aws_vpcs() {
  if [ -n "$VPC_ID" ]; then
    VPC_IDS="$VPC_ID"
    return
  fi

  printf "${C}No --vpc-id specified — fetching all VPCs in %s...${N}\n" "$REGION"
  local list
  list=$(ec2 describe-vpcs \
    --query 'Vpcs[].[VpcId,CidrBlock,Tags[?Key==`Name`].Value|[0]]' \
    --output text 2>&1) || { printf "${R}ERROR:${N} %s\n" "$list" >&2; exit 1; }

  VPC_IDS=""
  while IFS=$'\t' read -r _vid _vcidr _vname; do
    [ -z "$_vid" ] && continue
    VPC_IDS="$VPC_IDS $_vid"
    printf "  ${C}→${N} %-22s  %-20s  (%s)\n" "$_vid" "$_vcidr" "${_vname:-<no-name>}"
  done <<EOF
$list
EOF
  VPC_IDS=$(printf '%s' "$VPC_IDS" | tr -s ' ' | sed 's/^ //')

  if [ -z "$VPC_IDS" ]; then
    printf "${Y}No VPCs found in region %s.${N}\n" "$REGION"
    printf "${C}Continuing with region-level resource cleanup (snapshots, AMIs, ECR, key pairs)...${N}\n"
    return 0
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# Per-VPC cleanup — called once per VPC
# ══════════════════════════════════════════════════════════════════════════════
run_vpc_cleanup() {

# ──────────────────────────────────────────────────────────────────────────────
step "1/27" "EKS Clusters"
EKS_CLUSTERS_RAW=$(eks list-clusters --query 'clusters[]' --output text 2>/dev/null | tr '\t' '\n') || EKS_CLUSTERS_RAW=""
EKS_IN_VPC=""
while IFS= read -r _cname; do
  [ -z "$_cname" ] && continue
  _cvpc=$(eks describe-cluster --name "$_cname" \
    --query 'cluster.resourcesVpcConfig.vpcId' --output text 2>/dev/null) || _cvpc=""
  [ "$_cvpc" = "$VPC_ID" ] && EKS_IN_VPC="$EKS_IN_VPC $_cname"
done <<EOF
$EKS_CLUSTERS_RAW
EOF
EKS_IN_VPC=$(printf '%s' "$EKS_IN_VPC" | tr -s ' ' | sed 's/^ //')

if [ -z "$EKS_IN_VPC" ]; then
  none_msg
else
  for _cname in $EKS_IN_VPC; do
    found_msg "cluster: $_cname"
    _ngs=$(eks list-nodegroups --cluster-name "$_cname" \
      --query 'nodegroups[]' --output text 2>/dev/null | tr '\t' '\n') || _ngs=""
    while IFS= read -r _ng; do
      [ -z "$_ng" ] && continue
      found_msg "  node group: $_ng"
      run_delete "EKS node group $_ng" \
        aws --region "$REGION" eks delete-nodegroup --cluster-name "$_cname" --nodegroup-name "$_ng"
    done <<EOF
$_ngs
EOF
    _fps=$(eks list-fargate-profiles --cluster-name "$_cname" \
      --query 'fargateProfileNames[]' --output text 2>/dev/null | tr '\t' '\n') || _fps=""
    while IFS= read -r _fp; do
      [ -z "$_fp" ] && continue
      found_msg "  Fargate profile: $_fp"
      run_delete "EKS Fargate profile $_fp" \
        aws --region "$REGION" eks delete-fargate-profile --cluster-name "$_cname" --fargate-profile-name "$_fp"
    done <<EOF
$_fps
EOF
    if ! $DRY_RUN; then
      info_msg "waiting up to 10 min for node groups to delete..."
      eks wait nodegroup-deleted --cluster-name "$_cname" 2>/dev/null || true
    fi
    run_delete "EKS cluster $_cname" \
      aws --region "$REGION" eks delete-cluster --name "$_cname"
  done
fi

# ──────────────────────────────────────────────────────────────────────────────
step "2/27" "Auto Scaling Groups"
VPC_SUBNETS_FOR_ASG=$(ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'Subnets[].SubnetId' \
  --output text 2>/dev/null | tr '\t' ' ') || VPC_SUBNETS_FOR_ASG=""

ASG_NAMES=""
if [ -n "$VPC_SUBNETS_FOR_ASG" ]; then
  ALL_ASGS=$(asg describe-auto-scaling-groups \
    --query 'AutoScalingGroups[].[AutoScalingGroupName,VPCZoneIdentifier]' \
    --output text 2>/dev/null) || ALL_ASGS=""
  while IFS=$'\t' read -r _asg_name _asg_zones; do
    [ -z "$_asg_name" ] && continue
    for _sn in $VPC_SUBNETS_FOR_ASG; do
      if printf '%s' "$_asg_zones" | grep -qF "$_sn"; then
        ASG_NAMES="$ASG_NAMES $_asg_name"
        break
      fi
    done
  done <<EOF
$ALL_ASGS
EOF
  ASG_NAMES=$(printf '%s' "$ASG_NAMES" | tr -s ' ' | sed 's/^ //')
fi

if [ -z "$ASG_NAMES" ]; then
  none_msg
else
  for _asg in $ASG_NAMES; do
    found_msg "$_asg"
    run_delete "Auto Scaling Group $_asg" \
      aws --region "$REGION" autoscaling delete-auto-scaling-group \
        --auto-scaling-group-name "$_asg" --force-delete
  done
  if ! $DRY_RUN; then
    info_msg "waiting 30s for ASG deletions..."
    sleep 30
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "3/27" "EC2 Instances"
INSTANCES=$(ec2 describe-instances \
  --filters "Name=vpc-id,Values=$VPC_ID" \
            "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].[InstanceId,Tags[?Key==`Name`].Value|[0]]' \
  --output text 2>&1) || { err_msg "$INSTANCES"; INSTANCES=""; }

if [ -z "$INSTANCES" ] || printf '%s' "$INSTANCES" | grep -q "^$"; then
  none_msg
else
  INST_IDS=""
  while IFS=$'\t' read -r iid iname; do
    [ -z "$iid" ] && continue
    found_msg "$iid  (${iname:-<no-name>})"
    INST_IDS="$INST_IDS $iid"
  done <<EOF
$INSTANCES
EOF
  INST_IDS=$(printf '%s' "$INST_IDS" | tr -s ' ' | sed 's/^ //')
  if [ -n "$INST_IDS" ]; then
    KP_NAMES=$(ec2 describe-instances \
      --instance-ids $INST_IDS \
      --query 'Reservations[].Instances[].KeyName' \
      --output text 2>/dev/null | tr '\t' '\n' | grep -v '^$' | grep -v '^None$') || KP_NAMES=""
    for _kp in $KP_NAMES; do
      ALL_KEYPAIRS="$ALL_KEYPAIRS $_kp"
    done
    for iid in $INST_IDS; do
      ec2 modify-instance-attribute --instance-id "$iid" \
        --no-disable-api-termination > /dev/null 2>&1 || true
    done
    run_delete "terminate instances: $INST_IDS" \
      aws --region "$REGION" ec2 terminate-instances --instance-ids $INST_IDS
    if ! $DRY_RUN; then
      info_msg "waiting for all instances to reach terminated state..."
      ec2 wait instance-terminated --instance-ids $INST_IDS 2>&1 \
        && ok_msg "all instances terminated" \
        || err_msg "wait timed out — instances may still be terminating"
    fi
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "4/27" "NAT Gateways"
NAT_IDS=$(ec2 describe-nat-gateways \
  --filter "Name=vpc-id,Values=$VPC_ID" \
           "Name=state,Values=available,pending" \
  --query 'NatGateways[].NatGatewayId' \
  --output text 2>&1) || { err_msg "$NAT_IDS"; NAT_IDS=""; }
NAT_IDS=$(printf '%s' "$NAT_IDS" | tr '\t' ' ' | tr -s ' ' | sed 's/^ *//;s/ *$//')

if [ -z "$NAT_IDS" ]; then
  none_msg
else
  for nat in $NAT_IDS; do
    found_msg "$nat"
    run_delete "NAT Gateway $nat" aws --region "$REGION" ec2 delete-nat-gateway --nat-gateway-id "$nat"
  done
  if ! $DRY_RUN; then
    info_msg "waiting ~30s for NAT Gateways to delete..."
    sleep 30
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "5/27" "RDS Clusters + Instances"
RDS_SG_NAMES=$(rds describe-db-subnet-groups \
  --query "DBSubnetGroups[?VpcId=='$VPC_ID'].DBSubnetGroupName" \
  --output text 2>/dev/null | tr '\t' '\n' | grep -v '^$') || RDS_SG_NAMES=""

RDS_CLUSTER_IDS=""
RDS_INSTANCE_IDS=""
if [ -n "$RDS_SG_NAMES" ]; then
  ALL_CLUSTERS=$(rds describe-db-clusters \
    --query 'DBClusters[].[DBClusterIdentifier,Engine,Status,DBSubnetGroup]' \
    --output text 2>/dev/null) || ALL_CLUSTERS=""
  while IFS=$'\t' read -r _cid _eng _st _sg; do
    [ -z "$_cid" ] && continue
    printf '%s' "$RDS_SG_NAMES" | grep -qxF "$_sg" || continue
    [ "$_st" = "deleting" ] && continue
    found_msg "cluster: $_cid  engine=$_eng  status=$_st"
    RDS_CLUSTER_IDS="$RDS_CLUSTER_IDS $_cid"
  done <<EOF
$ALL_CLUSTERS
EOF
  ALL_INSTANCES=$(rds describe-db-instances \
    --query 'DBInstances[].[DBInstanceIdentifier,DBInstanceClass,DBInstanceStatus,DBSubnetGroup.DBSubnetGroupName,DBClusterIdentifier]' \
    --output text 2>/dev/null) || ALL_INSTANCES=""
  while IFS=$'\t' read -r _iid _cls _st _sg _cluster; do
    [ -z "$_iid" ] && continue
    printf '%s' "$RDS_SG_NAMES" | grep -qxF "$_sg" || continue
    [ "$_st" = "deleting" ] && continue
    [ -n "$_cluster" ] && [ "$_cluster" != "None" ] && continue
    found_msg "instance: $_iid  class=$_cls  status=$_st"
    RDS_INSTANCE_IDS="$RDS_INSTANCE_IDS $_iid"
  done <<EOF
$ALL_INSTANCES
EOF
fi
RDS_CLUSTER_IDS=$(printf '%s' "$RDS_CLUSTER_IDS" | tr -s ' ' | sed 's/^ //')
RDS_INSTANCE_IDS=$(printf '%s' "$RDS_INSTANCE_IDS" | tr -s ' ' | sed 's/^ //')

if [ -z "$RDS_CLUSTER_IDS" ] && [ -z "$RDS_INSTANCE_IDS" ]; then
  none_msg
else
  for _cid in $RDS_CLUSTER_IDS; do
    run_delete "RDS cluster $_cid" \
      aws --region "$REGION" rds delete-db-cluster \
        --db-cluster-identifier "$_cid" --skip-final-snapshot --delete-automated-backups
  done
  for _iid in $RDS_INSTANCE_IDS; do
    run_delete "RDS instance $_iid" \
      aws --region "$REGION" rds delete-db-instance \
        --db-instance-identifier "$_iid" --skip-final-snapshot --delete-automated-backups
  done
  if ! $DRY_RUN; then
    info_msg "waiting up to 15 min for RDS deletion..."
    for _cid in $RDS_CLUSTER_IDS; do
      rds wait db-cluster-deleted --db-cluster-identifier "$_cid" 2>/dev/null || true
    done
    for _iid in $RDS_INSTANCE_IDS; do
      rds wait db-instance-deleted --db-instance-identifier "$_iid" 2>/dev/null || true
    done
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "6/27" "ElastiCache Replication Groups + Clusters"
EC_SG_NAMES=$(ecache describe-cache-subnet-groups \
  --query "CacheSubnetGroups[?VpcId=='$VPC_ID'].CacheSubnetGroupName" \
  --output text 2>/dev/null | tr '\t' '\n' | grep -v '^$') || EC_SG_NAMES=""

EC_RG_IDS=""
EC_CLUSTER_IDS=""
if [ -n "$EC_SG_NAMES" ]; then
  ALL_RGS=$(ecache describe-replication-groups \
    --query 'ReplicationGroups[].[ReplicationGroupId,Status,CacheNodeType]' \
    --output text 2>/dev/null) || ALL_RGS=""
  while IFS=$'\t' read -r _rgid _st _type; do
    [ -z "$_rgid" ] && continue
    [ "$_st" = "deleting" ] && continue
    _member=$(ecache describe-replication-groups \
      --replication-group-id "$_rgid" \
      --query 'ReplicationGroups[0].MemberClusters[0]' \
      --output text 2>/dev/null) || _member=""
    [ -z "$_member" ] || [ "$_member" = "None" ] && continue
    _csg=$(ecache describe-cache-clusters \
      --cache-cluster-id "$_member" \
      --query 'CacheClusters[0].CacheSubnetGroup.CacheSubnetGroupName' \
      --output text 2>/dev/null) || _csg=""
    printf '%s' "$EC_SG_NAMES" | grep -qxF "$_csg" || continue
    found_msg "replication group: $_rgid  type=$_type  status=$_st"
    EC_RG_IDS="$EC_RG_IDS $_rgid"
  done <<EOF
$ALL_RGS
EOF
  ALL_EC_CLUSTERS=$(ecache describe-cache-clusters \
    --query 'CacheClusters[].[CacheClusterId,Engine,CacheClusterStatus,CacheSubnetGroup.CacheSubnetGroupName,ReplicationGroupId]' \
    --output text 2>/dev/null) || ALL_EC_CLUSTERS=""
  while IFS=$'\t' read -r _cid _eng _st _csg _rgid; do
    [ -z "$_cid" ] && continue
    printf '%s' "$EC_SG_NAMES" | grep -qxF "$_csg" || continue
    [ "$_st" = "deleting" ] && continue
    [ -n "$_rgid" ] && [ "$_rgid" != "None" ] && continue
    found_msg "cluster: $_cid  engine=$_eng  status=$_st"
    EC_CLUSTER_IDS="$EC_CLUSTER_IDS $_cid"
  done <<EOF
$ALL_EC_CLUSTERS
EOF
fi
EC_RG_IDS=$(printf '%s' "$EC_RG_IDS" | tr -s ' ' | sed 's/^ //')
EC_CLUSTER_IDS=$(printf '%s' "$EC_CLUSTER_IDS" | tr -s ' ' | sed 's/^ //')

if [ -z "$EC_RG_IDS" ] && [ -z "$EC_CLUSTER_IDS" ]; then
  none_msg
else
  for _rgid in $EC_RG_IDS; do
    run_delete "ElastiCache replication group $_rgid" \
      aws --region "$REGION" elasticache delete-replication-group \
        --replication-group-id "$_rgid" --no-retain-primary-cluster
  done
  for _cid in $EC_CLUSTER_IDS; do
    run_delete "ElastiCache cluster $_cid" \
      aws --region "$REGION" elasticache delete-cache-cluster --cache-cluster-id "$_cid"
  done
  if ! $DRY_RUN; then
    info_msg "waiting up to 10 min for ElastiCache deletion..."
    for _rgid in $EC_RG_IDS; do
      ecache wait replication-group-deleted --replication-group-id "$_rgid" 2>/dev/null || true
    done
    for _cid in $EC_CLUSTER_IDS; do
      ecache wait cache-cluster-deleted --cache-cluster-id "$_cid" 2>/dev/null || true
    done
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "7/27" "EFS Mount Targets → File Systems"
VPC_SUBNETS_EFS=$(ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'Subnets[].SubnetId' \
  --output text 2>/dev/null | tr '\t' ' ') || VPC_SUBNETS_EFS=""

EFS_MT_LIST=""
EFS_FS_LIST=""
ALL_FS=$(efs describe-file-systems \
  --query 'FileSystems[].[FileSystemId,LifeCycleState]' \
  --output text 2>/dev/null) || ALL_FS=""
while IFS=$'\t' read -r _fsid _state; do
  [ -z "$_fsid" ] && continue
  [ "$_state" = "deleting" ] && continue
  MT_DATA=$(efs describe-mount-targets \
    --file-system-id "$_fsid" \
    --query 'MountTargets[].[MountTargetId,SubnetId,LifeCycleState]' \
    --output text 2>/dev/null) || MT_DATA=""
  _found=false
  while IFS=$'\t' read -r _mtid _sn _mtstate; do
    [ -z "$_mtid" ] && continue
    for _vsn in $VPC_SUBNETS_EFS; do
      [ "$_sn" = "$_vsn" ] || continue
      found_msg "mount target: $_mtid  (fs=$_fsid  subnet=$_sn)"
      EFS_MT_LIST="$EFS_MT_LIST $_mtid:$_fsid"
      _found=true
      break
    done
  done <<EOF2
$MT_DATA
EOF2
  $_found && EFS_FS_LIST="$EFS_FS_LIST $_fsid"
done <<EOF
$ALL_FS
EOF
EFS_MT_LIST=$(printf '%s' "$EFS_MT_LIST" | tr -s ' ' | sed 's/^ //')
EFS_FS_LIST=$(printf '%s' "$EFS_FS_LIST" | tr -s ' ' | sed 's/^ //')

if [ -z "$EFS_MT_LIST" ]; then
  none_msg
else
  for _pair in $EFS_MT_LIST; do
    _mtid="${_pair%%:*}"
    run_delete "EFS mount target $_mtid" \
      aws --region "$REGION" efs delete-mount-target --mount-target-id "$_mtid"
  done
  if ! $DRY_RUN; then
    info_msg "waiting 20s for mount targets to delete..."
    sleep 20
  fi
  for _fsid in $EFS_FS_LIST; do
    found_msg "file system: $_fsid"
    run_delete "EFS file system $_fsid" \
      aws --region "$REGION" efs delete-file-system --file-system-id "$_fsid"
  done
fi

# ──────────────────────────────────────────────────────────────────────────────
step "8/27" "OpenSearch Domains"
OS_DOMAINS_RAW=$(opensearch list-domain-names \
  --query 'DomainNames[].DomainName' --output text 2>/dev/null | tr '\t' '\n') || OS_DOMAINS_RAW=""
OS_IN_VPC=""
while IFS= read -r _dname; do
  [ -z "$_dname" ] && continue
  _dvpc=$(opensearch describe-domain --domain-name "$_dname" \
    --query 'DomainStatus.VPCOptions.VPCId' --output text 2>/dev/null) || _dvpc=""
  [ "$_dvpc" = "$VPC_ID" ] && OS_IN_VPC="$OS_IN_VPC $_dname"
done <<EOF
$OS_DOMAINS_RAW
EOF
OS_IN_VPC=$(printf '%s' "$OS_IN_VPC" | tr -s ' ' | sed 's/^ //')

if [ -z "$OS_IN_VPC" ]; then
  none_msg
else
  for _dname in $OS_IN_VPC; do
    found_msg "$_dname"
    run_delete "OpenSearch domain $_dname" \
      aws --region "$REGION" opensearch delete-domain --domain-name "$_dname"
  done
  if ! $DRY_RUN; then
    info_msg "OpenSearch deletion is async — ENIs may take several minutes to release"
    sleep 30
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "9/27" "MSK (Kafka) Clusters"
MSK_DATA=$(kafka list-clusters-v2 \
  --query 'ClusterInfoList[].[ClusterArn,ClusterName,State]' \
  --output text 2>/dev/null) || MSK_DATA=""
MSK_IN_VPC=""
while IFS=$'\t' read -r _marn _mname _mstate; do
  [ -z "$_marn" ] && continue
  [ "$_mstate" = "DELETING" ] && continue
  _subnets=$(kafka describe-cluster-v2 --cluster-arn "$_marn" \
    --query 'ClusterInfo.Provisioned.BrokerNodeGroupInfo.ClientSubnets[]' \
    --output text 2>/dev/null | tr '\t' ' ') || _subnets=""
  [ -z "$_subnets" ] && continue
  for _vsn in $VPC_SUBNETS_EFS; do
    if printf '%s' "$_subnets" | grep -qF "$_vsn"; then
      found_msg "$_mname  ($_marn)  state=$_mstate"
      MSK_IN_VPC="$MSK_IN_VPC $_marn"
      break
    fi
  done
done <<EOF
$MSK_DATA
EOF
MSK_IN_VPC=$(printf '%s' "$MSK_IN_VPC" | tr -s ' ' | sed 's/^ //')

if [ -z "$MSK_IN_VPC" ]; then
  none_msg
else
  for _marn in $MSK_IN_VPC; do
    run_delete "MSK cluster $_marn" \
      aws --region "$REGION" kafka delete-cluster --cluster-arn "$_marn"
  done
  if ! $DRY_RUN; then
    info_msg "MSK deletion is async — ENIs may take several minutes to release"
    sleep 30
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "10/27" "Redshift Clusters"
RS_DATA=$(redshift describe-clusters \
  --query "Clusters[?VpcId=='$VPC_ID' && ClusterStatus!='deleting'].[ClusterIdentifier,NodeType,ClusterStatus,NumberOfNodes]" \
  --output text 2>/dev/null) || { err_msg "$RS_DATA"; RS_DATA=""; }

if [ -z "$RS_DATA" ] || printf '%s' "$RS_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r _rsid _type _st _nodes; do
    [ -z "$_rsid" ] && continue
    found_msg "$_rsid  type=$_type  status=$_st  nodes=$_nodes"
    run_delete "Redshift cluster $_rsid" \
      aws --region "$REGION" redshift delete-cluster \
        --cluster-identifier "$_rsid" --skip-final-cluster-snapshot
  done <<EOF
$RS_DATA
EOF
  if ! $DRY_RUN; then
    info_msg "Redshift deletion is async — may take several minutes"
    sleep 15
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "11/27" "EBS Volumes (detached/available)"
VPC_AZS=$(ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'Subnets[].AvailabilityZone' \
  --output text 2>/dev/null | tr '\t' ',') || VPC_AZS=""

EBS_IDS=""
if [ -n "$VPC_AZS" ]; then
  EBS_IDS=$(ec2 describe-volumes \
    --filters "Name=status,Values=available" \
              "Name=availability-zone,Values=$VPC_AZS" \
    --query 'Volumes[].[VolumeId,Tags[?Key==`Name`].Value|[0],Size]' \
    --output text 2>&1) || { err_msg "$EBS_IDS"; EBS_IDS=""; }
fi

if [ -z "$EBS_IDS" ] || printf '%s' "$EBS_IDS" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r vid vname vsize; do
    [ -z "$vid" ] && continue
    found_msg "$vid  ${vsize}GiB  (${vname:-<no-name>})"
    run_delete "EBS volume $vid" aws --region "$REGION" ec2 delete-volume --volume-id "$vid"
  done <<EOF
$EBS_IDS
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "12/27" "Elastic Network Interfaces (available)"
ENI_IDS=$(ec2 describe-network-interfaces \
  --filters "Name=vpc-id,Values=$VPC_ID" \
            "Name=status,Values=available" \
  --query 'NetworkInterfaces[].[NetworkInterfaceId,Description]' \
  --output text 2>&1) || { err_msg "$ENI_IDS"; ENI_IDS=""; }

if [ -z "$ENI_IDS" ] || printf '%s' "$ENI_IDS" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r eid edesc; do
    [ -z "$eid" ] && continue
    found_msg "$eid  (${edesc:-<no description>})"
    run_delete "ENI $eid" aws --region "$REGION" ec2 delete-network-interface --network-interface-id "$eid"
  done <<EOF
$ENI_IDS
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "13/27" "Elastic IPs (unassociated)"
EIP_DATA=$(ec2 describe-addresses \
  --filters "Name=domain,Values=vpc" \
  --query 'Addresses[?AssociationId==`null`].[AllocationId,PublicIp,Tags[?Key==`Name`].Value|[0]]' \
  --output text 2>&1) || { err_msg "$EIP_DATA"; EIP_DATA=""; }

if [ -z "$EIP_DATA" ] || printf '%s' "$EIP_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r alloc_id pub_ip ename; do
    [ -z "$alloc_id" ] && continue
    found_msg "$alloc_id  $pub_ip  (${ename:-<no-name>})"
    run_delete "EIP $alloc_id ($pub_ip)" \
      aws --region "$REGION" ec2 release-address --allocation-id "$alloc_id"
  done <<EOF
$EIP_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "14/27" "Load Balancers (ALB/NLB)"
LB_DATA=$(elb describe-load-balancers \
  --query "LoadBalancers[?VpcId=='$VPC_ID'].[LoadBalancerArn,LoadBalancerName]" \
  --output text 2>&1) || { err_msg "$LB_DATA"; LB_DATA=""; }

if [ -z "$LB_DATA" ] || printf '%s' "$LB_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r lb_arn lb_name; do
    [ -z "$lb_arn" ] && continue
    found_msg "$lb_name  ($lb_arn)"
    run_delete "LB $lb_name" aws --region "$REGION" elbv2 delete-load-balancer --load-balancer-arn "$lb_arn"
  done <<EOF
$LB_DATA
EOF
  if ! $DRY_RUN; then
    info_msg "waiting 10s for LB deletion..."
    sleep 10
  fi
fi

CLB_DATA=$(elb1 describe-load-balancers \
  --query "LoadBalancerDescriptions[?VPCId=='$VPC_ID'].LoadBalancerName" \
  --output text 2>&1) || CLB_DATA=""
if [ -n "$CLB_DATA" ] && ! printf '%s' "$CLB_DATA" | grep -q "^$"; then
  for clb in $CLB_DATA; do
    found_msg "Classic ELB: $clb"
    run_delete "Classic ELB $clb" aws --region "$REGION" elb delete-load-balancer --load-balancer-name "$clb"
  done
fi

# ──────────────────────────────────────────────────────────────────────────────
step "15/27" "EC2 Instance Connect Endpoints"
EICE_DATA=$(ec2 describe-instance-connect-endpoints \
  --filters "Name=vpc-id,Values=$VPC_ID" \
            "Name=state,Values=create-complete,create-in-progress,delete-in-progress" \
  --query 'InstanceConnectEndpoints[].[InstanceConnectEndpointId,State,SubnetId,Tags[?Key==`Name`].Value|[0]]' \
  --output text 2>&1) || { err_msg "$EICE_DATA"; EICE_DATA=""; }

if [ -z "$EICE_DATA" ] || printf '%s' "$EICE_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r eice_id eice_state eice_subnet eice_name; do
    [ -z "$eice_id" ] && continue
    found_msg "$eice_id  state=$eice_state  subnet=$eice_subnet  (${eice_name:-<no-name>})"
    run_delete "Instance Connect Endpoint $eice_id" \
      aws --region "$REGION" ec2 delete-instance-connect-endpoint \
        --instance-connect-endpoint-id "$eice_id"
  done <<EOF
$EICE_DATA
EOF
  if ! $DRY_RUN; then
    info_msg "waiting 20s for EICE ENIs to be fully released..."
    sleep 20
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "16/27" "VPC Endpoints"
EP_DATA=$(ec2 describe-vpc-endpoints \
  --filters "Name=vpc-id,Values=$VPC_ID" \
            "Name=vpc-endpoint-state,Values=available,pending,pending-acceptance,rejected" \
  --query 'VpcEndpoints[].[VpcEndpointId,ServiceName,VpcEndpointType]' \
  --output text 2>&1) || { err_msg "$EP_DATA"; EP_DATA=""; }

if [ -z "$EP_DATA" ] || printf '%s' "$EP_DATA" | grep -q "^$"; then
  none_msg
else
  EP_IDS=""
  while IFS=$'\t' read -r ep_id svc ep_type; do
    [ -z "$ep_id" ] && continue
    found_msg "$ep_id  $ep_type  $svc"
    EP_IDS="$EP_IDS $ep_id"
  done <<EOF
$EP_DATA
EOF
  EP_IDS=$(printf '%s' "$EP_IDS" | tr -s ' ' | sed 's/^ //')
  if [ -n "$EP_IDS" ]; then
    run_delete "VPC endpoints: $EP_IDS" \
      aws --region "$REGION" ec2 delete-vpc-endpoints --vpc-endpoint-ids $EP_IDS
    if ! $DRY_RUN; then
      info_msg "waiting 15s for endpoint deletion..."
      sleep 15
    fi
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "17/27" "Transit Gateway Attachments"
TGW_DATA=$(ec2 describe-transit-gateway-attachments \
  --filters "Name=resource-id,Values=$VPC_ID" \
  --query 'TransitGatewayAttachments[].[TransitGatewayAttachmentId,State,TransitGatewayId]' \
  --output text 2>&1) || { err_msg "$TGW_DATA"; TGW_DATA=""; }

if [ -z "$TGW_DATA" ] || printf '%s' "$TGW_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r att_id att_state tgw_id; do
    [ -z "$att_id" ] && continue
    found_msg "$att_id  state=$att_state  tgw=$tgw_id"
    case "$att_state" in
      deleting|deleted) skip_msg "$att_id already $att_state" ;;
      *) run_delete "TGW attachment $att_id" \
           aws --region "$REGION" ec2 delete-transit-gateway-vpc-attachment \
             --transit-gateway-attachment-id "$att_id" ;;
    esac
  done <<EOF
$TGW_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "18/27" "VPC Peering Connections"
PEER_REQ=$(ec2 describe-vpc-peering-connections \
  --filters "Name=requester-vpc-info.vpc-id,Values=$VPC_ID" \
            "Name=status-code,Values=active,pending-acceptance,provisioning,expired" \
  --query 'VpcPeeringConnections[].[VpcPeeringConnectionId,Status.Code]' \
  --output text 2>/dev/null) || PEER_REQ=""
PEER_ACC=$(ec2 describe-vpc-peering-connections \
  --filters "Name=accepter-vpc-info.vpc-id,Values=$VPC_ID" \
            "Name=status-code,Values=active,pending-acceptance,provisioning,expired" \
  --query 'VpcPeeringConnections[].[VpcPeeringConnectionId,Status.Code]' \
  --output text 2>/dev/null) || PEER_ACC=""
PEER_ALL=$(printf '%s\n%s' "$PEER_REQ" "$PEER_ACC" | sort -u | grep -v '^$') || PEER_ALL=""

if [ -z "$PEER_ALL" ]; then
  none_msg
else
  while IFS=$'\t' read -r peer_id peer_status; do
    [ -z "$peer_id" ] && continue
    found_msg "$peer_id  status=$peer_status"
    run_delete "VPC peering $peer_id" \
      aws --region "$REGION" ec2 delete-vpc-peering-connection --vpc-peering-connection-id "$peer_id"
  done <<EOF
$PEER_ALL
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "19/27" "VPN Connections"
VPN_VGW_IDS=$(ec2 describe-vpn-gateways \
  --filters "Name=attachment.vpc-id,Values=$VPC_ID" \
  --query 'VpnGateways[].VpnGatewayId' \
  --output text 2>/dev/null | tr '\t' ' ' | tr -s ' ' | sed 's/^ *//;s/ *$//') || VPN_VGW_IDS=""

VPN_DATA=""
if [ -n "$VPN_VGW_IDS" ]; then
  for _vgw in $VPN_VGW_IDS; do
    _chunk=$(ec2 describe-vpn-connections \
      --filters "Name=vpn-gateway-id,Values=$_vgw" \
                "Name=state,Values=available,pending" \
      --query 'VpnConnections[].[VpnConnectionId,State,Tags[?Key==`Name`].Value|[0]]' \
      --output text 2>&1) || _chunk=""
    [ -n "$_chunk" ] && VPN_DATA="${VPN_DATA}${_chunk}"$'\n'
  done
fi
VPN_DATA=$(printf '%s' "$VPN_DATA" | grep -v '^$') || VPN_DATA=""

if [ -z "$VPN_DATA" ]; then
  none_msg
else
  while IFS=$'\t' read -r vpn_id vpn_state vpn_name; do
    [ -z "$vpn_id" ] && continue
    found_msg "$vpn_id  state=$vpn_state  (${vpn_name:-<no-name>})"
    run_delete "VPN connection $vpn_id" \
      aws --region "$REGION" ec2 delete-vpn-connection --vpn-connection-id "$vpn_id"
  done <<EOF
$VPN_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "20/27" "Virtual Private Gateways"
VGW_DATA=$(ec2 describe-vpn-gateways \
  --filters "Name=attachment.vpc-id,Values=$VPC_ID" \
            "Name=state,Values=available,pending,attached" \
  --query 'VpnGateways[].[VpnGatewayId,State]' \
  --output text 2>&1) || { err_msg "$VGW_DATA"; VGW_DATA=""; }

if [ -z "$VGW_DATA" ] || printf '%s' "$VGW_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r vgw_id vgw_state; do
    [ -z "$vgw_id" ] && continue
    found_msg "$vgw_id  state=$vgw_state"
    if ! $DRY_RUN; then
      info_msg "detaching $vgw_id from $VPC_ID..."
      ec2 detach-vpn-gateway --vpn-gateway-id "$vgw_id" --vpc-id "$VPC_ID" > /dev/null 2>&1 || true
      sleep 5
    fi
    run_delete "VPN Gateway $vgw_id" \
      aws --region "$REGION" ec2 delete-vpn-gateway --vpn-gateway-id "$vgw_id"
  done <<EOF
$VGW_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "21/27" "Security Groups"
SG_DATA=$(ec2 describe-security-groups \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'SecurityGroups[].[GroupId,GroupName]' \
  --output text 2>&1) || { err_msg "$SG_DATA"; SG_DATA=""; }

if [ -z "$SG_DATA" ] || printf '%s' "$SG_DATA" | grep -q "^$"; then
  none_msg
else
  if ! $DRY_RUN; then
    info_msg "clearing all security group rules first..."
    while IFS=$'\t' read -r sg_id sg_name; do
      [ -z "$sg_id" ] && continue
      INGRESS=$(ec2 describe-security-groups --group-ids "$sg_id" \
        --query 'SecurityGroups[0].IpPermissions' --output json 2>/dev/null) || INGRESS="[]"
      if [ "$INGRESS" != "[]" ] && [ "$INGRESS" != "null" ] && [ -n "$INGRESS" ]; then
        ec2 revoke-security-group-ingress --group-id "$sg_id" --ip-permissions "$INGRESS" > /dev/null 2>&1 || true
      fi
      EGRESS=$(ec2 describe-security-groups --group-ids "$sg_id" \
        --query 'SecurityGroups[0].IpPermissionsEgress' --output json 2>/dev/null) || EGRESS="[]"
      if [ "$EGRESS" != "[]" ] && [ "$EGRESS" != "null" ] && [ -n "$EGRESS" ]; then
        ec2 revoke-security-group-egress --group-id "$sg_id" --ip-permissions "$EGRESS" > /dev/null 2>&1 || true
      fi
    done <<EOF2
$SG_DATA
EOF2
  fi
  while IFS=$'\t' read -r sg_id sg_name; do
    [ -z "$sg_id" ] && continue
    found_msg "$sg_id  ($sg_name)"
    if [ "$sg_name" = "default" ]; then
      skip_msg "$sg_id is the default SG — will be removed with the VPC"
    else
      run_delete "Security Group $sg_id ($sg_name)" \
        aws --region "$REGION" ec2 delete-security-group --group-id "$sg_id"
    fi
  done <<EOF
$SG_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "22/27" "Network ACLs (non-default)"
NACL_DATA=$(ec2 describe-network-acls \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'NetworkAcls[].[NetworkAclId,IsDefault]' \
  --output text 2>&1) || { err_msg "$NACL_DATA"; NACL_DATA=""; }

if [ -z "$NACL_DATA" ] || printf '%s' "$NACL_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r nacl_id is_default; do
    [ -z "$nacl_id" ] && continue
    found_msg "$nacl_id  default=$is_default"
    if [ "$is_default" = "True" ] || [ "$is_default" = "true" ]; then
      skip_msg "$nacl_id is default — removed with VPC"
    else
      run_delete "Network ACL $nacl_id" \
        aws --region "$REGION" ec2 delete-network-acl --network-acl-id "$nacl_id"
    fi
  done <<EOF
$NACL_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "23/27" "Route Tables (non-main)"
RT_DATA=$(ec2 describe-route-tables \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'RouteTables[].[RouteTableId,Associations[?Main==`true`]|length(@)]' \
  --output text 2>&1) || { err_msg "$RT_DATA"; RT_DATA=""; }

if [ -z "$RT_DATA" ] || printf '%s' "$RT_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r rt_id is_main_count; do
    [ -z "$rt_id" ] && continue
    found_msg "$rt_id  main=$( [ "$is_main_count" -gt 0 ] 2>/dev/null && printf 'yes' || printf 'no')"
    if [ "${is_main_count:-0}" -gt 0 ] 2>/dev/null; then
      skip_msg "$rt_id is the main route table — removed with VPC"
    else
      if ! $DRY_RUN; then
        ASSOCS=$(ec2 describe-route-tables \
          --route-table-ids "$rt_id" \
          --query 'RouteTables[0].Associations[?Main==`false`].RouteTableAssociationId' \
          --output text 2>/dev/null | tr '\t' ' ') || ASSOCS=""
        for assoc in $ASSOCS; do
          [ -z "$assoc" ] && continue
          info_msg "disassociating $assoc from $rt_id"
          ec2 disassociate-route-table --association-id "$assoc" > /dev/null 2>&1 || true
        done
      fi
      run_delete "Route Table $rt_id" \
        aws --region "$REGION" ec2 delete-route-table --route-table-id "$rt_id"
    fi
  done <<EOF
$RT_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "24/27" "Subnets"
SUBNET_DATA=$(ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'Subnets[].[SubnetId,CidrBlock,AvailabilityZone,Tags[?Key==`Name`].Value|[0]]' \
  --output text 2>&1) || { err_msg "$SUBNET_DATA"; SUBNET_DATA=""; }

if [ -z "$SUBNET_DATA" ] || printf '%s' "$SUBNET_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r sn_id cidr az sname; do
    [ -z "$sn_id" ] && continue
    found_msg "$sn_id  $cidr  $az  (${sname:-<no-name>})"
    run_delete "Subnet $sn_id" \
      aws --region "$REGION" ec2 delete-subnet --subnet-id "$sn_id"
  done <<EOF
$SUBNET_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "25/27" "Internet Gateways"
IGW_DATA=$(ec2 describe-internet-gateways \
  --filters "Name=attachment.vpc-id,Values=$VPC_ID" \
  --query 'InternetGateways[].[InternetGatewayId,Attachments[0].State]' \
  --output text 2>&1) || { err_msg "$IGW_DATA"; IGW_DATA=""; }

if [ -z "$IGW_DATA" ] || printf '%s' "$IGW_DATA" | grep -q "^$"; then
  none_msg
else
  while IFS=$'\t' read -r igw_id igw_state; do
    [ -z "$igw_id" ] && continue
    found_msg "$igw_id  attachment=$igw_state"
    if ! $DRY_RUN; then
      info_msg "detaching $igw_id from $VPC_ID..."
      ec2 detach-internet-gateway \
        --internet-gateway-id "$igw_id" --vpc-id "$VPC_ID" > /dev/null 2>&1 || true
    fi
    run_delete "Internet Gateway $igw_id" \
      aws --region "$REGION" ec2 delete-internet-gateway --internet-gateway-id "$igw_id"
  done <<EOF
$IGW_DATA
EOF
fi

# ──────────────────────────────────────────────────────────────────────────────
step "26/27" "DHCP Option Sets (custom)"
VPC_DHCP=$(ec2 describe-vpcs --vpc-ids "$VPC_ID" \
  --query 'Vpcs[0].DhcpOptionsId' --output text 2>/dev/null) || VPC_DHCP=""

if [ "$REGION" = "us-east-1" ]; then
  _DEFAULT_DHCP_DOMAIN="ec2.internal"
else
  _DEFAULT_DHCP_DOMAIN="${REGION}.compute.internal"
fi

if [ -z "$VPC_DHCP" ] || [ "$VPC_DHCP" = "None" ]; then
  none_msg
elif [ "$VPC_DHCP" = "default" ]; then
  found_msg "$VPC_DHCP  (already the AWS default — skip)"
  skip_msg "VPC is already using the AWS default DHCP options"
else
  DHCP_DOMAIN=$(ec2 describe-dhcp-options --dhcp-options-ids "$VPC_DHCP" \
    --query 'DhcpOptions[0].DhcpConfigurations[?Key==`domain-name`].Values[0].Value' \
    --output text 2>/dev/null) || DHCP_DOMAIN=""
  if printf '%s' "$DHCP_DOMAIN" | grep -qiF "$_DEFAULT_DHCP_DOMAIN"; then
    found_msg "$VPC_DHCP  (AWS default domain for $REGION — skip)"
    skip_msg "$VPC_DHCP is the AWS default DHCP set, will be released with VPC"
  else
    found_msg "$VPC_DHCP  (custom — domain: ${DHCP_DOMAIN:-unknown})"
    if ! $DRY_RUN; then
      ASSOC_VPCS=$(ec2 describe-vpcs \
        --filters "Name=dhcp-options-id,Values=$VPC_DHCP" \
        --query 'Vpcs[].VpcId' \
        --output text 2>/dev/null | tr '\t' ' ') || ASSOC_VPCS=""
      ASSOC_VPCS=$(printf '%s' "$ASSOC_VPCS" | tr -s ' ' | sed 's/^ *//;s/ *$//')
      for assoc_vpc in $ASSOC_VPCS; do
        ASSOC_ERR=$(ec2 associate-dhcp-options --dhcp-options-id default --vpc-id "$assoc_vpc" 2>&1)
        if [ $? -eq 0 ]; then ok_msg "reassociated $assoc_vpc → default"
        else err_msg "could not reassociate $assoc_vpc: $(printf '%s' "$ASSOC_ERR" | tail -1)"; fi
      done
      CURRENT_DHCP=$(ec2 describe-vpcs --vpc-ids "$VPC_ID" \
        --query 'Vpcs[0].DhcpOptionsId' --output text 2>/dev/null) || CURRENT_DHCP=""
      if [ "$CURRENT_DHCP" = "$VPC_DHCP" ]; then
        err_msg "VPC still associated with $VPC_DHCP — skipping delete"
      else
        ok_msg "$VPC_ID now uses: $CURRENT_DHCP"
        REMAINING=$(ec2 describe-vpcs \
          --filters "Name=dhcp-options-id,Values=$VPC_DHCP" \
          --query 'Vpcs[].VpcId' \
          --output text 2>/dev/null | tr '\t' ' ' | tr -s ' ' | sed 's/^ *//;s/ *$//') || REMAINING=""
        if [ -n "$REMAINING" ]; then
          err_msg "other VPCs still using $VPC_DHCP: $REMAINING — skipping delete"
        else
          run_delete "DHCP Option Set $VPC_DHCP" \
            aws --region "$REGION" ec2 delete-dhcp-options --dhcp-options-id "$VPC_DHCP"
        fi
      fi
    else
      printf "  ${Y}[dry-run]${N} would reassociate all VPCs off $VPC_DHCP then delete it\n"
    fi
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
step "27/27" "VPC: $VPC_ID"
if $DRY_RUN; then
  printf "  ${Y}[dry-run]${N} would delete VPC %s\n" "$VPC_ID"
else
  found_msg "$VPC_ID"
  ERR=$(ec2 delete-vpc --vpc-id "$VPC_ID" 2>&1)
  if [ $? -eq 0 ]; then
    ok_msg "VPC $VPC_ID deleted successfully"
  else
    err_msg "VPC deletion failed: $(printf '%s' "$ERR" | tail -1)"
    printf "\n${Y}${B}  ─── Remaining dependencies in %s ───${N}\n" "$VPC_ID"
    _check() {
      local label="$1" result; shift
      result=$(ec2 "$@" --output text 2>/dev/null | grep -v '^$' | tr '\n' ' ') || result=""
      [ -n "$result" ] && printf "  ${R}BLOCKED BY${N} %-35s %s\n" "$label:" "$result"
    }
    _check "Instances (non-terminated)" describe-instances \
      --filters "Name=vpc-id,Values=$VPC_ID" \
                "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down" \
      --query 'Reservations[].Instances[].InstanceId'
    _check "NAT Gateways" describe-nat-gateways \
      --filter "Name=vpc-id,Values=$VPC_ID" "Name=state,Values=available,pending,deleting" \
      --query 'NatGateways[].NatGatewayId'
    _check "Network Interfaces (in-use)" describe-network-interfaces \
      --filters "Name=vpc-id,Values=$VPC_ID" \
      --query 'NetworkInterfaces[].[NetworkInterfaceId,Status,Description]'
    _check "Security Groups (non-default)" describe-security-groups \
      --filters "Name=vpc-id,Values=$VPC_ID" \
      --query 'SecurityGroups[?GroupName!=`default`].GroupId'
    _check "Subnets" describe-subnets \
      --filters "Name=vpc-id,Values=$VPC_ID" --query 'Subnets[].SubnetId'
    _check "Internet Gateways" describe-internet-gateways \
      --filters "Name=attachment.vpc-id,Values=$VPC_ID" \
      --query 'InternetGateways[].InternetGatewayId'
    printf "\n  ${C}Re-run this script to retry, or resolve the above manually.${N}\n"
  fi
fi

} # end run_vpc_cleanup

# ══════════════════════════════════════════════════════════════════════════════
# run_aws_cleanup — entry point called by dispatcher
# ══════════════════════════════════════════════════════════════════════════════
run_aws_cleanup() {
  local _region_wide=false
  [ -z "$VPC_ID" ] && _region_wide=true

  resolve_aws_vpcs

  local _vpc_count
  _vpc_count=$(printf '%s' "$VPC_IDS" | wc -w | tr -d ' ')

  if [ "$_vpc_count" -eq 0 ]; then
    print_banner "aws" "$REGION" "Region-level resources only (no VPCs present)"
    confirm_or_abort "Region-level resources (snapshots, AMIs, ECR, key pairs) in $REGION"
  elif [ "$_vpc_count" -eq 1 ]; then
    print_banner "aws" "$REGION" "VPC: $VPC_IDS"
    confirm_or_abort "Resources inside VPC $VPC_IDS in $REGION"
  else
    print_banner "aws" "$REGION" "$_vpc_count VPCs (all in region)"
    confirm_or_abort "Resources inside ALL $_vpc_count VPCs in $REGION"
  fi

  ALL_KEYPAIRS=""
  local _vpc_idx=0
  for VPC_ID in $VPC_IDS; do
    _vpc_idx=$(( _vpc_idx + 1 ))
    if [ "$_vpc_count" -gt 1 ]; then
      printf "\n${B}╔══════════════════════════════════════════════════════════╗${N}\n"
      printf "${B}║  VPC %d of %d: %-43s║${N}\n" "$_vpc_idx" "$_vpc_count" "$VPC_ID "
      printf "${B}╚══════════════════════════════════════════════════════════╝${N}\n"
    fi
    run_vpc_cleanup
  done

  # ── Post-VPC region-level steps ──────────────────────────────────────────────
  step "+A" "EBS Snapshots (self-owned)"
  SNAP_DATA=$(ec2 describe-snapshots --owner-ids self \
    --query 'Snapshots[].[SnapshotId,VolumeSize,Description,StartTime]' \
    --output text 2>/dev/null) || SNAP_DATA=""
  if [ -z "$SNAP_DATA" ] || printf '%s' "$SNAP_DATA" | grep -q "^$"; then
    none_msg
  else
    while IFS=$'\t' read -r _snapid _size _desc _time; do
      [ -z "$_snapid" ] && continue
      found_msg "$_snapid  ${_size}GiB  ${_time}  (${_desc:-<no description>})"
      run_delete "Snapshot $_snapid" \
        aws --region "$REGION" ec2 delete-snapshot --snapshot-id "$_snapid"
    done <<EOF
$SNAP_DATA
EOF
  fi

  step "+B" "AMIs (self-owned)"
  AMI_DATA=$(ec2 describe-images --owners self \
    --query 'Images[].[ImageId,Name,CreationDate,BlockDeviceMappings[].Ebs.SnapshotId|join(`,`,@)]' \
    --output text 2>/dev/null) || AMI_DATA=""
  if [ -z "$AMI_DATA" ] || printf '%s' "$AMI_DATA" | grep -q "^$"; then
    none_msg
  else
    while IFS=$'\t' read -r _amid _aname _adate _snaps; do
      [ -z "$_amid" ] && continue
      found_msg "$_amid  $_aname  created=$_adate"
      run_delete "AMI $_amid" \
        aws --region "$REGION" ec2 deregister-image --image-id "$_amid"
      for _snap in $(printf '%s' "$_snaps" | tr ',' ' '); do
        [ -z "$_snap" ] || [ "$_snap" = "None" ] && continue
        run_delete "AMI backing snapshot $_snap" \
          aws --region "$REGION" ec2 delete-snapshot --snapshot-id "$_snap"
      done
    done <<EOF
$AMI_DATA
EOF
  fi

  step "+C" "ECR Repositories"
  ECR_DATA=$(ecr describe-repositories \
    --query 'repositories[].[repositoryName,createdAt]' \
    --output text 2>/dev/null) || ECR_DATA=""
  if [ -z "$ECR_DATA" ] || printf '%s' "$ECR_DATA" | grep -q "^$"; then
    none_msg
  else
    while IFS=$'\t' read -r _rname _rcreated; do
      [ -z "$_rname" ] && continue
      found_msg "$_rname  created=$_rcreated"
      run_delete "ECR repository $_rname" \
        aws --region "$REGION" ecr delete-repository --repository-name "$_rname" --force
    done <<EOF
$ECR_DATA
EOF
  fi

  if $_region_wide; then
    step "+D" "EC2 Key Pairs (all in region)"
    UNIQUE_KEYPAIRS=$(ec2 describe-key-pairs \
      --query 'KeyPairs[].KeyName' --output text 2>/dev/null \
      | tr '\t' '\n' | grep -v '^$') || UNIQUE_KEYPAIRS=""
  else
    step "+D" "EC2 Key Pairs (from terminated instances)"
    UNIQUE_KEYPAIRS=$(printf '%s' "$ALL_KEYPAIRS" | tr ' ' '\n' | sort -u | grep -v '^$') || UNIQUE_KEYPAIRS=""
  fi
  if [ -z "$UNIQUE_KEYPAIRS" ]; then
    none_msg
  else
    while IFS= read -r kp_name; do
      [ -z "$kp_name" ] && continue
      found_msg "$kp_name"
      run_delete "Key Pair $kp_name" \
        aws --region "$REGION" ec2 delete-key-pair --key-name "$kp_name"
    done <<EOF
$UNIQUE_KEYPAIRS
EOF
  fi
}
