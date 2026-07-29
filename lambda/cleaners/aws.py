"""
aws.py — AWSCleaner (mirrors lib/aws_cleanup.sh — 28 steps)
"""

import time
import boto3

from .base import BaseCleaner
from job_store import update_job, step_record


class AWSCleaner(BaseCleaner):
    TOTAL = 28

    def __init__(self, region: str, vpc_id: str, dry_run: bool,
                 table, job_id: str):
        super().__init__(table=table, job_id=job_id, dry_run=dry_run)
        self.region  = region
        self.vpc_id  = vpc_id

        # boto3 clients — all scoped to the target region
        self.ec2      = boto3.client("ec2",           region_name=region)
        self.elbv2    = boto3.client("elbv2",         region_name=region)
        self.elb      = boto3.client("elb",           region_name=region)
        self.asg      = boto3.client("autoscaling",   region_name=region)
        self.rds      = boto3.client("rds",           region_name=region)
        self.ecache   = boto3.client("elasticache",   region_name=region)
        self.efs      = boto3.client("efs",           region_name=region)
        self.eks      = boto3.client("eks",           region_name=region)
        self.opensearch = boto3.client("opensearch",  region_name=region)
        self.kafka    = boto3.client("kafka",         region_name=region)
        self.redshift = boto3.client("redshift",      region_name=region)
        self.ecr      = boto3.client("ecr",           region_name=region)

        self._collected_kp_names = []

    # ── helpers ───────────────────────────────────────────────────────────────

    def _delete(self, action_label: str, fn, **kwargs):
        """Call fn(**kwargs) unless dry_run; log result."""
        if self.dry_run:
            return f"[DRY-RUN] would delete: {action_label}"
        try:
            fn(**kwargs)
            return f"deleted: {action_label}"
        except Exception as exc:
            return f"error deleting {action_label}: {exc}"

    def _paginated(self, client, op_name: str, result_key: str, **kwargs) -> list:
        """Collect every page for a describe_* call. Several AWS calls default
        to a single page (up to 1000 items) — an account with more of a given
        resource type would otherwise silently see only the first page and
        leave the rest orphaned with no error surfaced."""
        paginator = client.get_paginator(op_name)
        out = []
        for page in paginator.paginate(**kwargs):
            out.extend(page[result_key])
        return out

    def _vpc_subnets(self) -> list[str]:
        r = self.ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [self.vpc_id]}]
        )
        return [s["SubnetId"] for s in r.get("Subnets", [])]

    def _vpc_list(self) -> list[str]:
        """Return [vpc_id] if specified, else all non-default VPCs in region.
        Returns None if the region is not enabled on this account."""
        if self.vpc_id:
            return [self.vpc_id]
        try:
            r = self.ec2.describe_vpcs()
        except self.ec2.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in ("AuthFailure", "UnauthorizedOperation",
                                                   "InvalidClientTokenId"):
                return None
            raise
        return [v["VpcId"] for v in r.get("Vpcs", [])
                if not v.get("IsDefault", False)]

    # ── Step implementations ──────────────────────────────────────────────────

    def step1_eks(self):
        self._emit(1, "EKS Clusters", "running")
        details = []
        try:
            clusters = self.eks.list_clusters().get("clusters", [])
            for name in clusters:
                info = self.eks.describe_cluster(name=name)["cluster"]
                if info.get("resourcesVpcConfig", {}).get("vpcId") != self.vpc_id:
                    continue
                # node groups
                ngs = self.eks.list_nodegroups(clusterName=name).get("nodegroups", [])
                for ng in ngs:
                    details.append(self._delete(f"EKS nodegroup {ng}",
                        self.eks.delete_nodegroup, clusterName=name, nodegroupName=ng))
                # fargate profiles
                fps = self.eks.list_fargate_profiles(clusterName=name).get("fargateProfileNames", [])
                for fp in fps:
                    details.append(self._delete(f"EKS fargate-profile {fp}",
                        self.eks.delete_fargate_profile, clusterName=name, fargateProfileName=fp))
                details.append(self._delete(f"EKS cluster {name}",
                    self.eks.delete_cluster, name=name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(1, "EKS Clusters", details)

    def step2_asg(self):
        self._emit(2, "Auto Scaling Groups", "running")
        details = []
        try:
            subnets = set(self._vpc_subnets())
            paginator = self.asg.get_paginator("describe_auto_scaling_groups")
            for page in paginator.paginate():
                for g in page["AutoScalingGroups"]:
                    zones = set(g.get("VPCZoneIdentifier", "").split(","))
                    if zones & subnets:
                        name = g["AutoScalingGroupName"]
                        details.append(self._delete(f"ASG {name}",
                            self.asg.delete_auto_scaling_group,
                            AutoScalingGroupName=name, ForceDelete=True))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(2, "Auto Scaling Groups", details)

    def step3_ec2(self):
        self._emit(3, "EC2 Instances", "running")
        details = []
        try:
            r = self.ec2.describe_instances(
                Filters=[
                    {"Name": "vpc-id",             "Values": [self.vpc_id]},
                    {"Name": "instance-state-name", "Values": ["pending","running","stopping","stopped"]},
                ]
            )
            ids = [i["InstanceId"]
                   for res in r["Reservations"]
                   for i   in res["Instances"]]
            if ids:
                # collect key pair names before terminating
                for res in r["Reservations"]:
                    for i in res["Instances"]:
                        kp = i.get("KeyName")
                        if kp:
                            self._collected_kp_names.append(kp)
                # Clear termination protection if set — otherwise TerminateInstances
                # fails with OperationNotPermitted and leaves the VPC un-deletable.
                if not self.dry_run:
                    for iid in ids:
                        try:
                            self.ec2.modify_instance_attribute(
                                InstanceId=iid,
                                DisableApiTermination={"Value": False},
                            )
                        except Exception as exc:
                            details.append(
                                f"error clearing termination protection on {iid}: {exc}"
                            )
                details.append(self._delete(f"EC2 instances {ids}",
                    self.ec2.terminate_instances, InstanceIds=ids))
                if not self.dry_run:
                    waiter = self.ec2.get_waiter("instance_terminated")
                    waiter.wait(InstanceIds=ids,
                                WaiterConfig={"Delay": 15, "MaxAttempts": 40})
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(3, "EC2 Instances", details)

    def step4_nat(self):
        self._emit(4, "NAT Gateways", "running")
        details = []
        try:
            r = self.ec2.describe_nat_gateways(
                Filters=[
                    {"Name": "vpc-id", "Values": [self.vpc_id]},
                    {"Name": "state",  "Values": ["available", "pending"]},
                ]
            )
            for nat in r.get("NatGateways", []):
                nid = nat["NatGatewayId"]
                details.append(self._delete(f"NAT {nid}",
                    self.ec2.delete_nat_gateway, NatGatewayId=nid))
            if details and not self.dry_run:
                time.sleep(30)
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(4, "NAT Gateways", details)

    def step5_rds(self):
        self._emit(5, "RDS Clusters + Instances", "running")
        details = []
        try:
            # find subnet groups in VPC
            sgs = {sg["DBSubnetGroupName"]
                   for sg in self.rds.describe_db_subnet_groups()["DBSubnetGroups"]
                   if sg.get("VpcId") == self.vpc_id}
            # clusters
            for c in self.rds.describe_db_clusters()["DBClusters"]:
                if c.get("DBSubnetGroup") in sgs and c["Status"] != "deleting":
                    cid = c["DBClusterIdentifier"]
                    details.append(self._delete(f"RDS cluster {cid}",
                        self.rds.delete_db_cluster,
                        DBClusterIdentifier=cid,
                        SkipFinalSnapshot=True,
                        DeleteAutomatedBackups=True))
            # standalone instances
            for i in self.rds.describe_db_instances()["DBInstances"]:
                sg = i.get("DBSubnetGroup", {}).get("DBSubnetGroupName", "")
                if sg in sgs and i["DBInstanceStatus"] != "deleting" \
                        and not i.get("DBClusterIdentifier"):
                    iid = i["DBInstanceIdentifier"]
                    details.append(self._delete(f"RDS instance {iid}",
                        self.rds.delete_db_instance,
                        DBInstanceIdentifier=iid,
                        SkipFinalSnapshot=True,
                        DeleteAutomatedBackups=True))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(5, "RDS Clusters + Instances", details)

    def step6_elasticache(self):
        self._emit(6, "ElastiCache", "running")
        details = []
        try:
            sgs = {sg["CacheSubnetGroupName"]
                   for sg in self.ecache.describe_cache_subnet_groups()["CacheSubnetGroups"]
                   if sg.get("VpcId") == self.vpc_id}
            for rg in self.ecache.describe_replication_groups()["ReplicationGroups"]:
                if rg["Status"] == "deleting":
                    continue
                member = (rg.get("MemberClusters") or [None])[0]
                if not member:
                    continue
                cc = self.ecache.describe_cache_clusters(CacheClusterId=member)["CacheClusters"]
                if cc and cc[0].get("CacheSubnetGroup", {}).get("CacheSubnetGroupName") in sgs:
                    rgid = rg["ReplicationGroupId"]
                    details.append(self._delete(f"ElastiCache RG {rgid}",
                        self.ecache.delete_replication_group,
                        ReplicationGroupId=rgid,
                        RetainPrimaryCluster=False))
            for c in self.ecache.describe_cache_clusters()["CacheClusters"]:
                sg = c.get("CacheSubnetGroup", {}).get("CacheSubnetGroupName", "")
                if sg in sgs and c["CacheClusterStatus"] != "deleting" \
                        and not c.get("ReplicationGroupId"):
                    cid = c["CacheClusterId"]
                    details.append(self._delete(f"ElastiCache cluster {cid}",
                        self.ecache.delete_cache_cluster, CacheClusterId=cid))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(6, "ElastiCache", details)

    def step7_efs(self):
        self._emit(7, "EFS", "running")
        details = []
        try:
            subnets = set(self._vpc_subnets())
            for fs in self.efs.describe_file_systems()["FileSystems"]:
                fsid = fs["FileSystemId"]
                if fs["LifeCycleState"] == "deleting":
                    continue
                mts = self.efs.describe_mount_targets(FileSystemId=fsid)["MountTargets"]
                in_vpc = [mt for mt in mts if mt.get("SubnetId") in subnets]
                if not in_vpc:
                    continue
                for mt in in_vpc:
                    mtid = mt["MountTargetId"]
                    details.append(self._delete(f"EFS mount-target {mtid}",
                        self.efs.delete_mount_target, MountTargetId=mtid))
                if not self.dry_run:
                    time.sleep(20)
                details.append(self._delete(f"EFS fs {fsid}",
                    self.efs.delete_file_system, FileSystemId=fsid))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(7, "EFS", details)

    def step8_opensearch(self):
        self._emit(8, "OpenSearch Domains", "running")
        details = []
        try:
            domains = self.opensearch.list_domain_names()["DomainNames"]
            for d in domains:
                name = d["DomainName"]
                info = self.opensearch.describe_domain(DomainName=name)["DomainStatus"]
                if info.get("VPCOptions", {}).get("VPCId") == self.vpc_id:
                    details.append(self._delete(f"OpenSearch domain {name}",
                        self.opensearch.delete_domain, DomainName=name))
            if details and not self.dry_run:
                time.sleep(30)
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(8, "OpenSearch Domains", details)

    def step9_msk(self):
        self._emit(9, "MSK (Kafka) Clusters", "running")
        details = []
        try:
            subnets = set(self._vpc_subnets())
            r = self.kafka.list_clusters_v2()
            for c in r.get("ClusterInfoList", []):
                if c.get("State") == "DELETING":
                    continue
                arn = c["ClusterArn"]
                info = self.kafka.describe_cluster_v2(ClusterArn=arn)["ClusterInfo"]
                client_subnets = set(
                    info.get("Provisioned", {})
                        .get("BrokerNodeGroupInfo", {})
                        .get("ClientSubnets", [])
                )
                if client_subnets & subnets:
                    details.append(self._delete(f"MSK cluster {arn}",
                        self.kafka.delete_cluster, ClusterArn=arn))
            if details and not self.dry_run:
                time.sleep(30)
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(9, "MSK (Kafka) Clusters", details)

    def step10_redshift(self):
        self._emit(10, "Redshift Clusters", "running")
        details = []
        try:
            for c in self.redshift.describe_clusters()["Clusters"]:
                if c.get("VpcId") == self.vpc_id and c["ClusterStatus"] != "deleting":
                    cid = c["ClusterIdentifier"]
                    details.append(self._delete(f"Redshift {cid}",
                        self.redshift.delete_cluster,
                        ClusterIdentifier=cid,
                        SkipFinalClusterSnapshot=True))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(10, "Redshift Clusters", details)

    def step11_ebs(self):
        self._emit(11, "EBS Volumes", "running")
        details = []
        try:
            azs = list({s["AvailabilityZone"]
                        for s in self.ec2.describe_subnets(
                            Filters=[{"Name": "vpc-id", "Values": [self.vpc_id]}]
                        )["Subnets"]})
            if azs:
                vols = self.ec2.describe_volumes(
                    Filters=[
                        {"Name": "availability-zone", "Values": azs},
                        {"Name": "status",            "Values": ["available"]},
                    ]
                )["Volumes"]
                for v in vols:
                    vid = v["VolumeId"]
                    details.append(self._delete(f"EBS volume {vid}",
                        self.ec2.delete_volume, VolumeId=vid))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(11, "EBS Volumes", details)

    def step12_alb(self):
        self._emit(12, "ALBs / NLBs / Classic ELBs", "running")
        details = []
        try:
            # Runs before ENI cleanup — an ALB/NLB owns its own ENIs
            # ("ELB net interface") that AWS refuses to force-delete directly
            # ("currently in use"); only deleting the load balancer itself
            # releases them.
            lbs = self.elbv2.describe_load_balancers()["LoadBalancers"]
            for lb in lbs:
                if lb.get("VpcId") == self.vpc_id:
                    arn = lb["LoadBalancerArn"]
                    # delete listeners first
                    listeners = self.elbv2.describe_listeners(
                        LoadBalancerArn=arn)["Listeners"]
                    for l_ in listeners:
                        self._delete(f"listener {l_['ListenerArn']}",
                            self.elbv2.delete_listener,
                            ListenerArn=l_["ListenerArn"])
                    details.append(self._delete(f"LB {arn}",
                        self.elbv2.delete_load_balancer, LoadBalancerArn=arn))
            # Classic ELBs
            classic = self.elb.describe_load_balancers()["LoadBalancerDescriptions"]
            for c in classic:
                if c.get("VPCId") == self.vpc_id:
                    name = c["LoadBalancerName"]
                    details.append(self._delete(f"Classic ELB {name}",
                        self.elb.delete_load_balancer, LoadBalancerName=name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(12, "ALBs / NLBs / Classic ELBs", details)

    def step13_target_groups(self):
        self._emit(13, "Target Groups", "running")
        details = []
        try:
            tgs = self.elbv2.describe_target_groups()["TargetGroups"]
            for tg in tgs:
                if tg.get("VpcId") == self.vpc_id:
                    arn = tg["TargetGroupArn"]
                    details.append(self._delete(f"target-group {arn}",
                        self.elbv2.delete_target_group, TargetGroupArn=arn))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(13, "Target Groups", details)

    def step14_vpc_endpoints(self):
        self._emit(14, "VPC Endpoints", "running")
        details = []
        try:
            # Runs before ENI cleanup — an Interface-type VPC Endpoint owns
            # its own ENIs, which AWS refuses to force-delete directly
            # ("currently in use"); only deleting the endpoint releases them,
            # and that's async (state goes to 'deleting', not instantly
            # 'deleted'), so wait for it here rather than letting the race
            # resurface one step later in ENI cleanup.
            eps = self.ec2.describe_vpc_endpoints(
                Filters=[{"Name": "vpc-id", "Values": [self.vpc_id]}]
            )["VpcEndpoints"]
            ids = [ep["VpcEndpointId"] for ep in eps
                   if ep["State"] not in ("deleted", "deleting")]
            if ids:
                details.append(self._delete(f"VPC endpoints {ids}",
                    self.ec2.delete_vpc_endpoints, VpcEndpointIds=ids))
                if not self.dry_run:
                    for _ in range(18):   # up to 90s (18 × 5s)
                        time.sleep(5)
                        remaining = self.ec2.describe_vpc_endpoints(
                            Filters=[
                                {"Name": "vpc-id", "Values": [self.vpc_id]},
                                {"Name": "vpc-endpoint-state", "Values": ["deleting"]},
                            ]
                        ).get("VpcEndpoints", [])
                        if not remaining:
                            break
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(14, "VPC Endpoints", details)

    def step15_eni(self):
        self._emit(15, "Network Interfaces (ENIs)", "running")
        details = []
        try:
            # GuardDuty Runtime Monitoring attaches managed ENIs (owned by
            # "amazon-aws") while EC2 instances are running in the VPC. Once
            # all instances are terminated (step 3 already waited for that),
            # GuardDuty retracts its ENIs automatically — but it can take a
            # few minutes. Poll here (up to 5 min) before trying to delete;
            # if they haven't gone by then, log a warning and continue anyway
            # so the remaining steps record the real error.
            def _gd_enis():
                return [
                    e for e in self.ec2.describe_network_interfaces(
                        Filters=[
                            {"Name": "vpc-id",                           "Values": [self.vpc_id]},
                            {"Name": "attachment.instance-owner-id",     "Values": ["amazon-aws"]},
                        ]
                    )["NetworkInterfaces"]
                    if "guardduty" in e.get("Description", "").lower()
                ]
            if not self.dry_run:
                pending = _gd_enis()
                if pending:
                    ids_str = ", ".join(e["NetworkInterfaceId"] for e in pending)
                    details.append(f"waiting for GuardDuty to retract ENIs: {ids_str}")
                    for _ in range(30):   # up to 5 minutes (30 × 10 s)
                        time.sleep(10)
                        if not _gd_enis():
                            details.append("GuardDuty ENIs retracted")
                            break
                    else:
                        remaining = _gd_enis()
                        if remaining:
                            ids_str = ", ".join(e["NetworkInterfaceId"] for e in remaining)
                            details.append(f"warn: GuardDuty ENIs still present after wait: {ids_str}")

            # Delete EC2 Instance Connect Endpoints — they hold
            # 'ec2_instance_connect_endpoint' ENIs that block subnet/VPC deletion.
            eice_list = self.ec2.describe_instance_connect_endpoints(
                Filters=[
                    {"Name": "vpc-id", "Values": [self.vpc_id]},
                    {"Name": "state",  "Values": ["create-complete", "create-in-progress"]},
                ]
            ).get("InstanceConnectEndpoints", [])
            if eice_list and not self.dry_run:
                for eice in eice_list:
                    eid = eice["InstanceConnectEndpointId"]
                    try:
                        self.ec2.delete_instance_connect_endpoint(
                            InstanceConnectEndpointId=eid)
                        details.append(f"deleted: Instance Connect Endpoint {eid}")
                    except Exception as exc:
                        details.append(f"warn deleting EICE {eid}: {exc}")
                # Wait up to 60s for deletion
                for _ in range(12):
                    time.sleep(5)
                    remaining = self.ec2.describe_instance_connect_endpoints(
                        Filters=[
                            {"Name": "vpc-id", "Values": [self.vpc_id]},
                            {"Name": "state",  "Values": ["delete-in-progress"]},
                        ]
                    ).get("InstanceConnectEndpoints", [])
                    if not remaining:
                        break
            elif eice_list and self.dry_run:
                for eice in eice_list:
                    details.append(f"[DRY-RUN] would delete: Instance Connect Endpoint {eice['InstanceConnectEndpointId']}")

            # Delete TGW VPC attachments first — they create 'ela-attach' ENIs
            # that cannot be detached directly and block subnet/VPC deletion.
            tgw_attachments = self.ec2.describe_transit_gateway_vpc_attachments(
                Filters=[
                    {"Name": "vpc-id",  "Values": [self.vpc_id]},
                    {"Name": "state",   "Values": ["available", "pending", "modifying"]},
                ]
            ).get("TransitGatewayVpcAttachments", [])
            if tgw_attachments and not self.dry_run:
                for att in tgw_attachments:
                    aid = att["TransitGatewayAttachmentId"]
                    try:
                        self.ec2.delete_transit_gateway_vpc_attachment(
                            TransitGatewayAttachmentId=aid)
                        details.append(f"deleted: TGW attachment {aid}")
                    except Exception as exc:
                        details.append(f"warn deleting TGW attachment {aid}: {exc}")
                # Wait up to 90s for all attachments to reach 'deleted'
                for _ in range(18):
                    time.sleep(5)
                    remaining = self.ec2.describe_transit_gateway_vpc_attachments(
                        Filters=[
                            {"Name": "vpc-id", "Values": [self.vpc_id]},
                            {"Name": "state",  "Values": ["deleting"]},
                        ]
                    ).get("TransitGatewayVpcAttachments", [])
                    if not remaining:
                        break
            elif tgw_attachments and self.dry_run:
                for att in tgw_attachments:
                    details.append(f"[DRY-RUN] would delete: TGW attachment {att['TransitGatewayAttachmentId']}")

            enis = self._paginated(self.ec2, "describe_network_interfaces", "NetworkInterfaces",
                Filters=[{"Name": "vpc-id", "Values": [self.vpc_id]}])
            for eni in enis:
                eid = eni["NetworkInterfaceId"]
                if eni["Status"] == "in-use":
                    att = eni.get("Attachment", {})
                    att_id = att.get("AttachmentId")
                    # Only attempt detach for non-ela-attach types
                    if att_id and att.get("InstanceOwnerId") != "amazon-aws" \
                            and not self.dry_run:
                        try:
                            self.ec2.detach_network_interface(
                                AttachmentId=att_id, Force=True)
                            for _ in range(10):
                                time.sleep(3)
                                r = self.ec2.describe_network_interfaces(
                                    NetworkInterfaceIds=[eid])["NetworkInterfaces"]
                                if not r or r[0]["Status"] == "available":
                                    break
                        except Exception as exc:
                            details.append(f"warn detaching {eid}: {exc}")
                details.append(self._delete(f"ENI {eid}",
                    self.ec2.delete_network_interface, NetworkInterfaceId=eid))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(15, "Network Interfaces (ENIs)", details)

    def step16_eip(self):
        self._emit(16, "Elastic IPs", "running")
        details = []
        try:
            addrs = self.ec2.describe_addresses(
                Filters=[{"Name": "domain", "Values": ["vpc"]}]
            )["Addresses"]
            # only release unassociated ones (associated ones may be held by active infra)
            for a in addrs:
                if not a.get("AssociationId"):
                    alloc_id = a.get("AllocationId")
                    if alloc_id:
                        details.append(self._delete(f"EIP {alloc_id}",
                            self.ec2.release_address, AllocationId=alloc_id))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(16, "Elastic IPs", details)

    def step17_security_groups(self):
        self._emit(17, "Security Groups", "running")
        details = []
        try:
            sgs = self._paginated(self.ec2, "describe_security_groups", "SecurityGroups",
                Filters=[{"Name": "vpc-id", "Values": [self.vpc_id]}])
            # clear all ingress/egress rules first (break cross-references)
            if not self.dry_run:
                for sg in sgs:
                    sgid = sg["GroupId"]
                    if sg["GroupName"] == "default":
                        continue
                    if sg.get("IpPermissions"):
                        self.ec2.revoke_security_group_ingress(
                            GroupId=sgid, IpPermissions=sg["IpPermissions"])
                    if sg.get("IpPermissionsEgress"):
                        self.ec2.revoke_security_group_egress(
                            GroupId=sgid, IpPermissions=sg["IpPermissionsEgress"])
            for sg in sgs:
                if sg["GroupName"] == "default":
                    continue
                sgid = sg["GroupId"]
                details.append(self._delete(f"SG {sgid} ({sg['GroupName']})",
                    self.ec2.delete_security_group, GroupId=sgid))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(17, "Security Groups", details)

    def step18_nacls(self):
        self._emit(18, "Network ACLs", "running")
        details = []
        try:
            nacls = self.ec2.describe_network_acls(
                Filters=[{"Name": "vpc-id", "Values": [self.vpc_id]}]
            )["NetworkAcls"]
            for nacl in nacls:
                if nacl.get("IsDefault"):
                    continue
                nid = nacl["NetworkAclId"]
                details.append(self._delete(f"NACL {nid}",
                    self.ec2.delete_network_acl, NetworkAclId=nid))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(18, "Network ACLs", details)

    def step19_route_tables(self):
        self._emit(19, "Route Tables", "running")
        details = []
        try:
            rts = self._paginated(self.ec2, "describe_route_tables", "RouteTables",
                Filters=[{"Name": "vpc-id", "Values": [self.vpc_id]}])
            for rt in rts:
                # skip main route table
                if any(a.get("Main") for a in rt.get("Associations", [])):
                    continue
                rtid = rt["RouteTableId"]
                # disassociate subnets first
                if not self.dry_run:
                    for assoc in rt.get("Associations", []):
                        if not assoc.get("Main"):
                            self.ec2.disassociate_route_table(
                                AssociationId=assoc["RouteTableAssociationId"])
                details.append(self._delete(f"route-table {rtid}",
                    self.ec2.delete_route_table, RouteTableId=rtid))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(19, "Route Tables", details)

    def step20_subnets(self):
        self._emit(20, "Subnets", "running")
        details = []
        try:
            subnets = self.ec2.describe_subnets(
                Filters=[{"Name": "vpc-id", "Values": [self.vpc_id]}]
            )["Subnets"]
            for s in subnets:
                sid = s["SubnetId"]
                details.append(self._delete(f"subnet {sid}",
                    self.ec2.delete_subnet, SubnetId=sid))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(20, "Subnets", details)

    def step21_igw(self):
        self._emit(21, "Internet Gateways", "running")
        details = []
        try:
            igws = self.ec2.describe_internet_gateways(
                Filters=[{"Name": "attachment.vpc-id", "Values": [self.vpc_id]}]
            )["InternetGateways"]
            for igw in igws:
                igwid = igw["InternetGatewayId"]
                if not self.dry_run:
                    self.ec2.detach_internet_gateway(
                        InternetGatewayId=igwid, VpcId=self.vpc_id)
                details.append(self._delete(f"IGW {igwid}",
                    self.ec2.delete_internet_gateway, InternetGatewayId=igwid))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(21, "Internet Gateways", details)

    def step22_vpc(self):
        self._emit(22, "VPC", "running")
        details = []
        try:
            details.append(self._delete(f"VPC {self.vpc_id}",
                self.ec2.delete_vpc, VpcId=self.vpc_id))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(22, "VPC", details)

    def step23_snapshots(self):
        self._emit(23, "EBS Snapshots (owned by account)", "running")
        details = []
        try:
            snaps = self._paginated(self.ec2, "describe_snapshots", "Snapshots",
                OwnerIds=["self"])
            for s in snaps:
                sid = s["SnapshotId"]
                details.append(self._delete(f"snapshot {sid}",
                    self.ec2.delete_snapshot, SnapshotId=sid))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(23, "EBS Snapshots", details)

    def step24_amis(self):
        self._emit(24, "AMIs (owned by account)", "running")
        details = []
        try:
            images = self._paginated(self.ec2, "describe_images", "Images",
                Owners=["self"])
            for img in images:
                iid = img["ImageId"]
                details.append(self._delete(f"AMI {iid}",
                    self.ec2.deregister_image, ImageId=iid))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(24, "AMIs", details)

    def step25_ecr(self):
        self._emit(25, "ECR Repositories", "running")
        details = []
        try:
            repos = self.ecr.describe_repositories()["repositories"]
            for repo in repos:
                name = repo["repositoryName"]
                details.append(self._delete(f"ECR repo {name}",
                    self.ecr.delete_repository,
                    repositoryName=name, force=True))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(25, "ECR Repositories", details)

    def step26_key_pairs(self):
        self._emit(26, "Key Pairs", "running")
        details = []
        try:
            for kp_name in set(self._collected_kp_names):
                details.append(self._delete(f"key pair {kp_name}",
                    self.ec2.delete_key_pair, KeyName=kp_name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(26, "Key Pairs", details)

    def step27_subnet_groups(self):
        """Delete orphaned DB / cache subnet groups."""
        self._emit(27, "Orphaned Subnet Groups", "running")
        details = []
        try:
            for sg in self.rds.describe_db_subnet_groups()["DBSubnetGroups"]:
                if sg.get("VpcId") == self.vpc_id and sg["DBSubnetGroupName"] != "default":
                    details.append(self._delete(
                        f"DB subnet group {sg['DBSubnetGroupName']}",
                        self.rds.delete_db_subnet_group,
                        DBSubnetGroupName=sg["DBSubnetGroupName"]))
            for sg in self.ecache.describe_cache_subnet_groups()["CacheSubnetGroups"]:
                if sg.get("VpcId") == self.vpc_id:
                    details.append(self._delete(
                        f"cache subnet group {sg['CacheSubnetGroupName']}",
                        self.ecache.delete_cache_subnet_group,
                        CacheSubnetGroupName=sg["CacheSubnetGroupName"]))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(27, "Orphaned Subnet Groups", details)

    def step28_transit_gateways(self):
        """Delete Aviatrix-created Transit Gateways once they have no
        remaining attachments. TGW is a region-level resource (not scoped
        to a single VPC), so this runs once per region after all VPCs have
        been processed rather than inside the per-VPC loop."""
        self._emit(28, "Transit Gateways", "running")
        details = []
        try:
            tgws = self.ec2.describe_transit_gateways(
                Filters=[{"Name": "state", "Values": ["available", "pending", "modifying"]}]
            ).get("TransitGateways", [])
            for tgw in tgws:
                tags = {t["Key"]: t["Value"] for t in tgw.get("Tags", [])}
                is_avx = any("aviatrix" in k.lower() or "aviatrix" in v.lower()
                             for k, v in tags.items())
                if not is_avx:
                    continue
                tid = tgw["TransitGatewayId"]
                remaining = self.ec2.describe_transit_gateway_attachments(
                    Filters=[
                        {"Name": "transit-gateway-id", "Values": [tid]},
                        {"Name": "state", "Values": [
                            "available", "pending", "modifying", "pendingAcceptance"]},
                    ]
                ).get("TransitGatewayAttachments", [])
                if remaining:
                    ids = [a["TransitGatewayAttachmentId"] for a in remaining]
                    details.append(
                        f"skip TGW {tid}: still has attachments {ids}")
                    continue
                details.append(self._delete(f"Transit Gateway {tid}",
                    self.ec2.delete_transit_gateway, TransitGatewayId=tid))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(28, "Transit Gateways", details)

    def run(self):
        vpcs = self._vpc_list()
        if vpcs is None:
            update_job(self.table, self.job_id, "COMPLETE",
                       step_record(1, 1, "Region check", "skipped",
                                   f"Region {self.region} is not enabled on this account"))
            return
        for vpc in vpcs:
            self.vpc_id = vpc
            self.step1_eks()
            self.step2_asg()
            self.step3_ec2()
            self.step4_nat()
            self.step5_rds()
            self.step6_elasticache()
            self.step7_efs()
            self.step8_opensearch()
            self.step9_msk()
            self.step10_redshift()
            self.step11_ebs()
            self.step12_alb()
            self.step13_target_groups()
            self.step14_vpc_endpoints()
            self.step15_eni()
            self.step16_eip()
            self.step17_security_groups()
            self.step18_nacls()
            self.step19_route_tables()
            self.step20_subnets()
            self.step21_igw()
            self.step22_vpc()
            self.step23_snapshots()
            self.step24_amis()
            self.step25_ecr()
            self.step26_key_pairs()
            self.step27_subnet_groups()
        self.step28_transit_gateways()
