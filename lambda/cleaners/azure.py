"""
azure.py — AzureCleaner (mirrors lib/azure_cleanup.sh — 22 per-RG steps + 4 region-level)
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .base import BaseCleaner, CSP_IGNORE_KEY, CSP_IGNORE_VALUE


class AzureCleaner(BaseCleaner):
    TOTAL = 26

    def __init__(self, region: str, dry_run: bool, table, job_id: str,
                 credential, subscription_id: str):
        super().__init__(table=table, job_id=job_id, dry_run=dry_run)
        self.region          = region
        self.subscription_id = subscription_id

        # Imports are local so cold starts on AWS-only requests don't pay for them.
        from azure.mgmt.resource import ResourceManagementClient
        from azure.mgmt.resource.locks import ManagementLockClient
        from azure.mgmt.compute import ComputeManagementClient
        from azure.mgmt.network import NetworkManagementClient
        from azure.mgmt.sql import SqlManagementClient
        from azure.mgmt.rdbms.postgresql_flexibleservers import PostgreSQLManagementClient
        from azure.mgmt.rdbms.mysql_flexibleservers import MySQLManagementClient
        from azure.mgmt.redis import RedisManagementClient
        from azure.mgmt.search import SearchManagementClient
        from azure.mgmt.eventhub import EventHubManagementClient
        from azure.mgmt.synapse import SynapseManagementClient
        from azure.mgmt.containerregistry import ContainerRegistryManagementClient
        from azure.mgmt.containerservice import ContainerServiceClient

        kw = {"credential": credential, "subscription_id": subscription_id}
        self.resource = ResourceManagementClient(**kw)
        self.locks    = ManagementLockClient(**kw)
        self.compute  = ComputeManagementClient(**kw)
        self.network  = NetworkManagementClient(**kw)
        self.sql      = SqlManagementClient(**kw)
        self.pg       = PostgreSQLManagementClient(**kw)
        self.mysql    = MySQLManagementClient(**kw)
        self.redis    = RedisManagementClient(**kw)
        self.search   = SearchManagementClient(**kw)
        self.eh       = EventHubManagementClient(**kw)
        self.synapse  = SynapseManagementClient(**kw)
        self.acr      = ContainerRegistryManagementClient(**kw)
        self.aks      = ContainerServiceClient(**kw)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _az_delete(self, action_label: str, fn, *args, _wait_timeout: int = 180, **kwargs):
        """Run fn(*args, **kwargs) unless dry_run; if it returns an LROPoller,
        wait up to _wait_timeout seconds for completion. Always returns a
        descriptive string; never raises."""
        if self.dry_run:
            return f"[DRY-RUN] would delete: {action_label}"
        try:
            result = fn(*args, **kwargs)
            if hasattr(result, "result"):
                result.result(timeout=_wait_timeout)
            return f"deleted: {action_label}"
        except Exception as exc:
            return f"error deleting {action_label}: {exc}"

    def _az_delete_parallel(self, calls: list, _wait_timeout: int = 180) -> list[str]:
        """Start every begin_delete call first, then wait on all the returned
        LROPollers concurrently. Serializing N resources' delete-and-wait
        (each can take minutes) into one loop risks exceeding the worker
        Lambda's timeout; starting them all up front and waiting together
        bounds the total wait to the slowest one instead of the sum.
        calls: list of (label, fn, args, kwargs). Never raises."""
        if self.dry_run:
            return [f"[DRY-RUN] would delete: {label}" for label, *_ in calls]
        if not calls:
            return []

        started = []  # (label, poller_or_exception)
        for label, fn, args, kwargs in calls:
            try:
                started.append((label, fn(*args, **kwargs)))
            except Exception as exc:
                started.append((label, exc))

        def _wait(item):
            label, poller = item
            if isinstance(poller, Exception):
                return f"error deleting {label}: {poller}"
            try:
                if hasattr(poller, "result"):
                    poller.result(timeout=_wait_timeout)
                return f"deleted: {label}"
            except Exception as exc:
                return f"error deleting {label}: {exc}"

        with ThreadPoolExecutor(max_workers=min(len(started), 10)) as ex:
            return list(ex.map(_wait, started))

    def _az_fire_and_forget(self, action_label: str, fn, *args, **kwargs):
        """Initiate a delete but don't wait — used for RG deletion where the
        bash script uses --no-wait. Returns descriptive string; never raises."""
        if self.dry_run:
            return f"[DRY-RUN] would delete: {action_label}"
        try:
            fn(*args, **kwargs)
            return f"deletion initiated: {action_label}"
        except Exception as exc:
            return f"error initiating {action_label}: {exc}"

    def _list_rgs(self) -> list:
        """Return RGs in self.region that are Aviatrix-created: either a tag key
        contains 'aviatrix' (e.g. 'Aviatrix-Created-Resource'), or the RG name
        itself starts with 'avx-' (the Aviatrix controller's own RG, e.g.
        'avx-controller_group', carries no tags at all)."""
        out = []
        for rg in self.resource.resource_groups.list():
            if (rg.location or "").lower() != self.region.lower():
                continue
            tag_keys_lower = [(k or "").lower() for k in (rg.tags or {})]
            name_lower = (rg.name or "").lower()
            if any("aviatrix" in k for k in tag_keys_lower) or name_lower.startswith("avx-"):
                out.append(rg)
        return out

    def _rg_protected_reason(self, rg) -> str | None:
        """Return a reason string if this resource group (or any VM inside
        it) is tagged csp-cost-ignore=yes, else None. Azure RG deletion
        (step22) cascades to remove every resource inside regardless of
        which per-resource steps ran earlier, so per-VM skips alone can't
        protect a gateway/controller — only skipping the whole RG can."""
        if self._is_ignore_tag(rg.tags):
            return f"resource group tagged {CSP_IGNORE_KEY}={CSP_IGNORE_VALUE}"
        try:
            names = [vm.name for vm in self.compute.virtual_machines.list(rg.name)
                     if self._is_ignore_tag(vm.tags)]
            if names:
                return (f"protected VM(s) tagged "
                        f"{CSP_IGNORE_KEY}={CSP_IGNORE_VALUE}: {names}")
        except Exception as exc:
            return f"error checking {CSP_IGNORE_KEY} tag: {exc}"
        return None

    def _vnets(self, rg_name: str) -> list[str]:
        try:
            return [v.name for v in self.network.virtual_networks.list(rg_name)]
        except Exception:
            return []

    # ── Per-resource-group steps ──────────────────────────────────────────────

    def step1_locks(self, rg: str):
        self._emit(1, f"Resource Locks ({rg})", "running")
        details = []
        try:
            locks = list(self.locks.management_locks.list_at_resource_group_level(rg))
            for lock in locks:
                details.append(self._az_delete(
                    f"Lock {lock.name}",
                    self.locks.management_locks.delete_at_resource_group_level,
                    rg, lock.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(1, f"Resource Locks ({rg})", details)

    def step2_aks(self, rg: str):
        self._emit(2, f"AKS Clusters ({rg})", "running")
        details = []
        try:
            clusters = list(self.aks.managed_clusters.list_by_resource_group(rg))
            for c in clusters:
                if (c.provisioning_state or "").lower() == "deleting":
                    details.append(f"skipped: {c.name} already deleting")
                    continue
                # Delete user-managed agent pools first (system pool deletes with cluster)
                try:
                    pools = list(self.aks.agent_pools.list(rg, c.name))
                except Exception:
                    pools = []
                for p in pools:
                    if (p.name or "").lower() == "system":
                        continue
                    details.append(self._az_delete(
                        f"AKS node pool {p.name} in {c.name}",
                        self.aks.agent_pools.begin_delete, rg, c.name, p.name,
                        _wait_timeout=600))
                details.append(self._az_delete(
                    f"AKS cluster {c.name}",
                    self.aks.managed_clusters.begin_delete, rg, c.name,
                    _wait_timeout=900))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(2, f"AKS Clusters ({rg})", details)

    def step3_vmss(self, rg: str):
        self._emit(3, f"VM Scale Sets ({rg})", "running")
        details = []
        try:
            calls = []
            for v in self.compute.virtual_machine_scale_sets.list(rg):
                if (v.provisioning_state or "").lower() == "deleting":
                    details.append(f"skipped: {v.name} already deleting")
                    continue
                calls.append((f"VMSS {v.name}",
                    self.compute.virtual_machine_scale_sets.begin_delete,
                    (rg, v.name), {"force_deletion": True}))
            details.extend(self._az_delete_parallel(calls, _wait_timeout=600))
            if details and not self.dry_run:
                time.sleep(30)
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(3, f"VM Scale Sets ({rg})", details)

    def step4_vms(self, rg: str):
        self._emit(4, f"Virtual Machines ({rg})", "running")
        details = []
        try:
            calls = [
                (f"VM {vm.name}", self.compute.virtual_machines.begin_delete,
                 (rg, vm.name), {"force_deletion": True})
                for vm in self.compute.virtual_machines.list(rg)
            ]
            details.extend(self._az_delete_parallel(calls, _wait_timeout=600))
            if details and not self.dry_run:
                time.sleep(20)
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(4, f"Virtual Machines ({rg})", details)

    def step5_disks(self, rg: str):
        self._emit(5, f"Managed Disks unattached ({rg})", "running")
        details = []
        try:
            for d in self.compute.disks.list_by_resource_group(rg):
                if (d.disk_state or "") != "Unattached":
                    continue
                details.append(self._az_delete(
                    f"Managed Disk {d.name}",
                    self.compute.disks.begin_delete, rg, d.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(5, f"Managed Disks unattached ({rg})", details)

    def step6_nat_gateways(self, rg: str):
        self._emit(6, f"NAT Gateways ({rg})", "running")
        details = []
        try:
            for n in self.network.nat_gateways.list(rg):
                details.append(self._az_delete(
                    f"NAT Gateway {n.name}",
                    self.network.nat_gateways.begin_delete, rg, n.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(6, f"NAT Gateways ({rg})", details)

    def step7_sql(self, rg: str):
        self._emit(7, f"SQL + PG/MySQL Flexible ({rg})", "running")
        details = []
        try:
            # Azure SQL Servers — delete DBs (except master) before server
            for srv in self.sql.servers.list_by_resource_group(rg):
                try:
                    dbs = list(self.sql.databases.list_by_server(rg, srv.name))
                except Exception:
                    dbs = []
                for db in dbs:
                    if db.name == "master":
                        continue
                    details.append(self._az_delete(
                        f"SQL DB {db.name} on {srv.name}",
                        self.sql.databases.begin_delete, rg, srv.name, db.name))
                details.append(self._az_delete(
                    f"SQL Server {srv.name}",
                    self.sql.servers.begin_delete, rg, srv.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        # PostgreSQL flexible servers
        try:
            for s in self.pg.servers.list_by_resource_group(rg):
                details.append(self._az_delete(
                    f"postgres flexible-server {s.name}",
                    self.pg.servers.begin_delete, rg, s.name))
        except Exception as exc:
            details.append(f"error postgres: {exc}")
        # MySQL flexible servers
        try:
            for s in self.mysql.servers.list_by_resource_group(rg):
                details.append(self._az_delete(
                    f"mysql flexible-server {s.name}",
                    self.mysql.servers.begin_delete, rg, s.name))
        except Exception as exc:
            details.append(f"error mysql: {exc}")
        self._finalize(7, f"SQL + PG/MySQL Flexible ({rg})", details)

    def step8_redis(self, rg: str):
        self._emit(8, f"Redis Caches ({rg})", "running")
        details = []
        try:
            calls = []
            for r in self.redis.redis.list_by_resource_group(rg):
                if (r.provisioning_state or "").lower() == "deleting":
                    details.append(f"skipped: {r.name} already deleting")
                    continue
                calls.append((f"Redis cache {r.name}",
                    self.redis.redis.begin_delete, (rg, r.name), {}))
            details.extend(self._az_delete_parallel(calls, _wait_timeout=600))
            if details and not self.dry_run:
                time.sleep(30)
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(8, f"Redis Caches ({rg})", details)

    def step9_search(self, rg: str):
        self._emit(9, f"Cognitive Search ({rg})", "running")
        details = []
        try:
            for s in self.search.services.list_by_resource_group(rg):
                details.append(self._az_delete(
                    f"Search service {s.name}",
                    self.search.services.delete, rg, s.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(9, f"Cognitive Search ({rg})", details)

    def step10_event_hubs(self, rg: str):
        self._emit(10, f"Event Hubs Namespaces ({rg})", "running")
        details = []
        try:
            for ns in self.eh.namespaces.list_by_resource_group(rg):
                details.append(self._az_delete(
                    f"Event Hubs namespace {ns.name}",
                    self.eh.namespaces.begin_delete, rg, ns.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(10, f"Event Hubs Namespaces ({rg})", details)

    def step11_synapse(self, rg: str):
        self._emit(11, f"Synapse Workspaces ({rg})", "running")
        details = []
        try:
            for w in self.synapse.workspaces.list_by_resource_group(rg):
                details.append(self._az_delete(
                    f"Synapse workspace {w.name}",
                    self.synapse.workspaces.begin_delete, rg, w.name,
                    _wait_timeout=600))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(11, f"Synapse Workspaces ({rg})", details)

    def step12_nics(self, rg: str):
        self._emit(12, f"Network Interfaces detached ({rg})", "running")
        details = []
        try:
            for n in self.network.network_interfaces.list(rg):
                if n.virtual_machine is not None:
                    continue
                details.append(self._az_delete(
                    f"NIC {n.name}",
                    self.network.network_interfaces.begin_delete, rg, n.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(12, f"Network Interfaces detached ({rg})", details)

    def step13_public_ips(self, rg: str):
        self._emit(13, f"Public IPs unassociated ({rg})", "running")
        details = []
        try:
            for p in self.network.public_ip_addresses.list(rg):
                if p.ip_configuration is not None:
                    continue
                details.append(self._az_delete(
                    f"Public IP {p.name}",
                    self.network.public_ip_addresses.begin_delete, rg, p.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(13, f"Public IPs unassociated ({rg})", details)

    def step14_lbs(self, rg: str):
        self._emit(14, f"Load Balancers + App Gateways ({rg})", "running")
        details = []
        try:
            calls = [
                (f"Load Balancer {lb.name}", self.network.load_balancers.begin_delete,
                 (rg, lb.name), {})
                for lb in self.network.load_balancers.list(rg)
            ]
            details.extend(self._az_delete_parallel(calls))
        except Exception as exc:
            details.append(f"error LBs: {exc}")
        try:
            calls = [
                (f"Application Gateway {ag.name}", self.network.application_gateways.begin_delete,
                 (rg, ag.name), {})
                for ag in self.network.application_gateways.list(rg)
            ]
            details.extend(self._az_delete_parallel(calls, _wait_timeout=600))
        except Exception as exc:
            details.append(f"error AGWs: {exc}")
        self._finalize(14, f"Load Balancers + App Gateways ({rg})", details)

    def step15_private_endpoints(self, rg: str):
        self._emit(15, f"Private Endpoints ({rg})", "running")
        details = []
        try:
            for pe in self.network.private_endpoints.list(rg):
                details.append(self._az_delete(
                    f"Private Endpoint {pe.name}",
                    self.network.private_endpoints.begin_delete, rg, pe.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(15, f"Private Endpoints ({rg})", details)

    def step16_vnet_peerings(self, rg: str):
        self._emit(16, f"VNet Peerings ({rg})", "running")
        details = []
        try:
            for vnet in self._vnets(rg):
                try:
                    peerings = list(self.network.virtual_network_peerings.list(rg, vnet))
                except Exception:
                    peerings = []
                for p in peerings:
                    details.append(self._az_delete(
                        f"VNet peering {p.name} in {vnet}",
                        self.network.virtual_network_peerings.begin_delete,
                        rg, vnet, p.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(16, f"VNet Peerings ({rg})", details)

    def step17_vpn_gateways(self, rg: str):
        self._emit(17, f"VPN Gateway + Connections ({rg})", "running")
        details = []
        try:
            gateways = list(self.network.virtual_network_gateways.list(rg))
            try:
                conns = list(self.network.virtual_network_gateway_connections.list(rg))
            except Exception:
                conns = []
            gw_ids = {(gw.id or "") for gw in gateways}
            # Connections must go before their gateway, but connections for
            # different gateways are independent — delete+wait all of them
            # together first, then all gateways together.
            conn_calls = [
                (f"VPN connection {conn.name}",
                 self.network.virtual_network_gateway_connections.begin_delete,
                 (rg, conn.name), {})
                for conn in conns
                if getattr(conn, "virtual_network_gateway1", None)
                and (conn.virtual_network_gateway1.id or "") in gw_ids
            ]
            details.extend(self._az_delete_parallel(conn_calls))

            gw_calls = [
                (f"VPN Gateway {gw.name}", self.network.virtual_network_gateways.begin_delete,
                 (rg, gw.name), {})
                for gw in gateways
            ]
            details.extend(self._az_delete_parallel(gw_calls, _wait_timeout=900))
            if gateways and not self.dry_run:
                time.sleep(30)
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(17, f"VPN Gateway + Connections ({rg})", details)

    def step18_nsgs(self, rg: str):
        self._emit(18, f"Network Security Groups ({rg})", "running")
        details = []
        try:
            for nsg in self.network.network_security_groups.list(rg):
                details.append(self._az_delete(
                    f"NSG {nsg.name}",
                    self.network.network_security_groups.begin_delete,
                    rg, nsg.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(18, f"Network Security Groups ({rg})", details)

    def step19_route_tables(self, rg: str):
        self._emit(19, f"Route Tables ({rg})", "running")
        details = []
        try:
            for rt in self.network.route_tables.list(rg):
                details.append(self._az_delete(
                    f"Route Table {rt.name}",
                    self.network.route_tables.begin_delete, rg, rt.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(19, f"Route Tables ({rg})", details)

    def step20_subnets(self, rg: str):
        self._emit(20, f"Subnets ({rg})", "running")
        details = []
        try:
            for vnet in self._vnets(rg):
                try:
                    subnets = list(self.network.subnets.list(rg, vnet))
                except Exception:
                    subnets = []
                for sn in subnets:
                    details.append(self._az_delete(
                        f"Subnet {sn.name} in {vnet}",
                        self.network.subnets.begin_delete, rg, vnet, sn.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(20, f"Subnets ({rg})", details)

    def step21_vnets(self, rg: str):
        self._emit(21, f"Virtual Networks ({rg})", "running")
        details = []
        try:
            for vnet in self._vnets(rg):
                details.append(self._az_delete(
                    f"VNet {vnet}",
                    self.network.virtual_networks.begin_delete, rg, vnet))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(21, f"Virtual Networks ({rg})", details)

    def step22_resource_group(self, rg: str):
        self._emit(22, f"Resource Group {rg}", "running")
        details = [self._az_fire_and_forget(
            f"Resource Group {rg}",
            self.resource.resource_groups.begin_delete, rg)]
        self._finalize(22, f"Resource Group {rg}", details)

    # ── Region-level steps (after all RGs) ────────────────────────────────────

    def _in_region(self, resource) -> bool:
        return (getattr(resource, "location", "") or "").lower() == self.region.lower()

    @staticmethod
    def _rg_from_id(resource_id: str) -> str:
        # /subscriptions/{sub}/resourceGroups/{rg}/providers/...
        parts = (resource_id or "").split("/")
        for i, p in enumerate(parts):
            if p.lower() == "resourcegroups" and i + 1 < len(parts):
                return parts[i + 1]
        return ""

    def step23_snapshots(self):
        self._emit(23, "Snapshots (region)", "running")
        details = []
        try:
            for s in self.compute.snapshots.list():
                if not self._in_region(s):
                    continue
                rg = self._rg_from_id(s.id)
                if not rg:
                    continue
                details.append(self._az_delete(
                    f"Snapshot {s.name} (rg={rg})",
                    self.compute.snapshots.begin_delete, rg, s.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(23, "Snapshots (region)", details)

    def step24_images(self):
        self._emit(24, "Custom Images + SIG (region)", "running")
        details = []
        # Plain custom images
        try:
            for img in self.compute.images.list():
                if not self._in_region(img):
                    continue
                rg = self._rg_from_id(img.id)
                if not rg:
                    continue
                details.append(self._az_delete(
                    f"Image {img.name} (rg={rg})",
                    self.compute.images.begin_delete, rg, img.name))
        except Exception as exc:
            details.append(f"error images: {exc}")
        # Shared Image Galleries: versions → definitions → galleries
        try:
            for gal in self.compute.galleries.list():
                if not self._in_region(gal):
                    continue
                rg = self._rg_from_id(gal.id)
                if not rg:
                    continue
                try:
                    defs = list(self.compute.gallery_images.list_by_gallery(rg, gal.name))
                except Exception:
                    defs = []
                for d in defs:
                    try:
                        vers = list(self.compute.gallery_image_versions.list_by_gallery_image(
                            rg, gal.name, d.name))
                    except Exception:
                        vers = []
                    for v in vers:
                        details.append(self._az_delete(
                            f"Image version {v.name} in {d.name}",
                            self.compute.gallery_image_versions.begin_delete,
                            rg, gal.name, d.name, v.name))
                    details.append(self._az_delete(
                        f"Image definition {d.name}",
                        self.compute.gallery_images.begin_delete,
                        rg, gal.name, d.name))
                details.append(self._az_delete(
                    f"Shared Image Gallery {gal.name}",
                    self.compute.galleries.begin_delete, rg, gal.name))
        except Exception as exc:
            details.append(f"error galleries: {exc}")
        self._finalize(24, "Custom Images + SIG (region)", details)

    def step25_acr(self):
        self._emit(25, "Container Registries (region)", "running")
        details = []
        try:
            for r in self.acr.registries.list():
                if not self._in_region(r):
                    continue
                rg = self._rg_from_id(r.id)
                if not rg:
                    continue
                details.append(self._az_delete(
                    f"ACR {r.name} (rg={rg})",
                    self.acr.registries.begin_delete, rg, r.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(25, "Container Registries (region)", details)

    def step26_ssh_keys(self):
        self._emit(26, "SSH Public Keys (region)", "running")
        details = []
        try:
            for k in self.compute.ssh_public_keys.list_by_subscription():
                if not self._in_region(k):
                    continue
                rg = self._rg_from_id(k.id)
                if not rg:
                    continue
                details.append(self._az_delete(
                    f"SSH Key {k.name} (rg={rg})",
                    self.compute.ssh_public_keys.delete, rg, k.name))
        except Exception as exc:
            details.append(f"error: {exc}")
        self._finalize(26, "SSH Public Keys (region)", details)

    # ── Orchestration ────────────────────────────────────────────────────────

    def run(self):
        rgs = self._list_rgs()
        if not rgs:
            # Emit a single info step so the user sees why nothing happened
            self._emit(1, f"Resource Groups in {self.region}",
                       "skipped",
                       f"no RGs in {self.region} are tagged 'aviatrix' or named 'avx-*'")
            # Still run region-level scans — there may be orphaned snapshots/images/ACRs
            self.step23_snapshots()
            self.step24_images()
            self.step25_acr()
            self.step26_ssh_keys()
            return

        for rg in rgs:
            name = rg.name
            reason = self._rg_protected_reason(rg)
            if reason:
                self._emit(1, f"Resource Group {name} — protected, skipping",
                           "skipped", reason)
                continue
            self.step1_locks(name)
            self.step2_aks(name)
            self.step3_vmss(name)
            self.step4_vms(name)
            self.step5_disks(name)
            self.step6_nat_gateways(name)
            self.step7_sql(name)
            self.step8_redis(name)
            self.step9_search(name)
            self.step10_event_hubs(name)
            self.step11_synapse(name)
            self.step12_nics(name)
            self.step13_public_ips(name)
            self.step14_lbs(name)
            self.step15_private_endpoints(name)
            self.step16_vnet_peerings(name)
            self.step17_vpn_gateways(name)
            self.step18_nsgs(name)
            # Subnets must go before route tables — Azure refuses to delete
            # an RT while a subnet still references it (InUseRouteTableCannotBeDeleted).
            self.step20_subnets(name)
            self.step19_route_tables(name)
            self.step21_vnets(name)
            self.step22_resource_group(name)

        self.step23_snapshots()
        self.step24_images()
        self.step25_acr()
        self.step26_ssh_keys()
