"""
gcp.py — GCPCleaner (14 steps — filters by aviatrix label)
Uses google-auth + raw REST so no extra layer is needed.
"""

import time

from .base import BaseCleaner


class GCPCleaner(BaseCleaner):
    TOTAL     = 14
    AVX_LABEL = "aviatrix-created-resource"
    BASE      = "https://compute.googleapis.com/compute/v1"
    GKE_BASE  = "https://container.googleapis.com/v1"

    def __init__(self, region: str, dry_run: bool, table, job_id: str,
                 credentials, project_id: str, is_primary_region: bool = False):
        super().__init__(table=table, job_id=job_id, dry_run=dry_run)
        self.region  = region
        self.project = project_id
        # Only the primary region worker deletes global GCP resources
        # (firewalls, routes, VPC networks). All others skip those steps
        # to prevent parallel workers from racing on the same endpoints.
        self.is_primary_region = is_primary_region

        from google.auth.transport.requests import AuthorizedSession
        self.s = AuthorizedSession(credentials)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get(self, url: str, params: dict = None) -> dict:
        r = self.s.get(url, params=params or {})
        r.raise_for_status()
        return r.json()

    def _dbg(self, msg: str):
        print(f"[GCP-DEBUG][{self.region}] {msg}", flush=True)

    def _delete_url(self, label: str, url: str) -> str:
        if self.dry_run:
            self._dbg(f"DRY-RUN skip: {label}")
            return f"[DRY-RUN] would delete: {label}"
        self._dbg(f"DELETE {label}  url={url}")
        try:
            r = self.s.delete(url)
            self._dbg(f"  -> {r.status_code}  body={r.text[:400]}")
            if r.status_code == 404:
                return f"already gone: {label}"
            if r.status_code == 400 and "protected against deletion" in r.text:
                self._dbg(f"  -> deletion protection set, disabling...")
                dp_result = self._disable_deletion_protection(url)
                self._dbg(f"  -> disable-protection result: {dp_result}")
                r = self.s.delete(url)
                self._dbg(f"  -> retry {r.status_code}  body={r.text[:400]}")
            r.raise_for_status()
            op = r.json()
            self._wait_op(op)
            return f"deleted: {label}"
        except Exception as exc:
            self._dbg(f"  -> EXCEPTION: {exc}")
            return f"error deleting {label}: {exc}"

    def _disable_deletion_protection(self, instance_url: str) -> str:
        try:
            patch_url = f"{instance_url}/setDeletionProtection?deletionProtection=false"
            self._dbg(f"  POST {patch_url}")
            r = self.s.post(patch_url)
            self._dbg(f"  -> {r.status_code}  body={r.text[:300]}")
            if r.status_code not in (200, 202, 204):
                return f"failed: {r.status_code} {r.text[:200]}"
            op = r.json()
            if op.get("kind", "").endswith("Operation"):
                self._wait_op(op)
            return "ok"
        except Exception as exc:
            return f"exception: {exc}"

    def _wait_op(self, op: dict, timeout: int = 300):
        deadline = time.time() + timeout
        name = op.get("name", "")
        zone_url   = op.get("zone", "")
        region_url = op.get("region", "")
        while time.time() < deadline:
            if zone_url:
                zone = zone_url.rsplit("/", 1)[-1]
                url  = f"{self.BASE}/projects/{self.project}/zones/{zone}/operations/{name}"
            elif region_url:
                reg = region_url.rsplit("/", 1)[-1]
                url = f"{self.BASE}/projects/{self.project}/regions/{reg}/operations/{name}"
            else:
                url = f"{self.BASE}/projects/{self.project}/global/operations/{name}"
            done = self._get(url)
            if done.get("status") == "DONE":
                err = done.get("error")
                if err:
                    raise RuntimeError(str(err))
                return
            time.sleep(5)
        raise TimeoutError(f"GCP operation {name} timed out")

    def _avx_filter(self) -> str:
        # Aviatrix GCP resources use name prefix "avx-"; they carry no labels.
        return "name:avx-"

    def _avx_networks(self) -> list:
        try:
            data = self._get(f"{self.BASE}/projects/{self.project}/global/networks")
            return [n["name"] for n in data.get("items", []) if self._is_avx(n.get("name", ""))]
        except Exception:
            return []

    def _list(self, url: str, params: dict = None) -> list:
        try:
            data = self._get(url, params)
            return data.get("items", [])
        except Exception as exc:
            return [{"_list_error": str(exc)}]

    def _aggregated_instances_raw(self) -> list:
        """Return all instances in self.region as (name, zone, network_name) tuples.
        No server-side filter — GCP aggregated API ignores name: filters silently."""
        try:
            data = self._get(
                f"{self.BASE}/projects/{self.project}/aggregated/instances",
            )
            out = []
            for zone_key, zone_data in data.get("items", {}).items():
                zone = zone_key.removeprefix("zones/")
                if not zone.startswith(self.region):
                    continue
                for inst in zone_data.get("instances", []):
                    nics = inst.get("networkInterfaces", [])
                    net = nics[0].get("network", "").rsplit("/", 1)[-1] if nics else ""
                    out.append((inst["name"], zone, net))
            return out
        except Exception as exc:
            return [("_error", str(exc), "")]

    def _aggregated_disks_raw(self) -> list:
        """Return all disks in self.region as (name, zone) tuples. No server-side filter."""
        try:
            data = self._get(
                f"{self.BASE}/projects/{self.project}/aggregated/disks",
            )
            out = []
            for zone_key, zone_data in data.get("items", {}).items():
                zone = zone_key.removeprefix("zones/")
                if not zone.startswith(self.region):
                    continue
                for disk in zone_data.get("disks", []):
                    out.append((disk["name"], zone))
            return out
        except Exception as exc:
            return [("_error", str(exc))]

    # ── Steps ─────────────────────────────────────────────────────────────────

    def step1_gke(self):
        details = []
        try:
            data = self._get(
                f"{self.GKE_BASE}/projects/{self.project}/locations/-/clusters"
            )
            for c in data.get("clusters", []):
                loc = c.get("location", "")
                if not loc.startswith(self.region):
                    continue
                if ("aviatrix" not in c.get("name", "").lower()
                        and self.AVX_LABEL not in (c.get("resourceLabels") or {})):
                    continue
                cname = c["name"]
                url   = f"{self.GKE_BASE}/projects/{self.project}/locations/{loc}/clusters/{cname}"
                details.append(self._delete_url(f"GKE cluster {cname}", url))
        except Exception as exc:
            details.append(f"error listing GKE clusters: {exc}")
        self._finalize(1, "GKE Clusters", details)

    def step2_instances(self, avx_nets: set):
        details = []
        for iname, zone, net in self._aggregated_instances_raw():
            if iname == "_error":
                details.append(f"error listing instances: {zone}")
                continue
            is_avx_name = self._is_avx(iname)
            in_avx_net  = net in avx_nets
            self._dbg(f"instance {iname} zone={zone} net={net} is_avx_name={is_avx_name} in_avx_net={in_avx_net}")
            if not (is_avx_name or in_avx_net):
                self._dbg(f"  -> SKIP (not avx)")
                continue
            url = f"{self.BASE}/projects/{self.project}/zones/{zone}/instances/{iname}"
            details.append(self._delete_url(f"instance {iname} ({zone})", url))
        self._finalize(2, "Compute Instances", details)

    def step3_disks(self, avx_nets: set):
        details = []
        for dname, zone in self._aggregated_disks_raw():
            if dname == "_error":
                details.append(f"error listing disks: {zone}")
                continue
            if not self._is_avx(dname):
                continue
            url = f"{self.BASE}/projects/{self.project}/zones/{zone}/disks/{dname}"
            details.append(self._delete_url(f"disk {dname} ({zone})", url))
        self._finalize(3, "Persistent Disks", details)

    def step4_instance_groups(self, avx_nets: set):
        details = []
        try:
            data = self._get(
                f"{self.BASE}/projects/{self.project}/aggregated/instanceGroups",
            )
            for zone_key, zone_data in data.get("items", {}).items():
                zone = zone_key.removeprefix("zones/")
                if not zone.startswith(self.region):
                    continue
                for ig in zone_data.get("instanceGroups", []):
                    igname = ig["name"]
                    net    = ig.get("network", "").rsplit("/", 1)[-1]
                    if not (self._is_avx(igname) or net in avx_nets):
                        continue
                    url = f"{self.BASE}/projects/{self.project}/zones/{zone}/instanceGroups/{igname}"
                    details.append(self._delete_url(f"instance group {igname} ({zone})", url))
        except Exception as exc:
            details.append(f"error listing instance groups: {exc}")
        self._finalize(4, "Instance Groups", details)

    def step5_forwarding_rules(self):
        details = []
        for fr in self._list(
            f"{self.BASE}/projects/{self.project}/regions/{self.region}/forwardingRules",
            {"filter": self._avx_filter()},
        ):
            fname = fr["name"]
            url   = f"{self.BASE}/projects/{self.project}/regions/{self.region}/forwardingRules/{fname}"
            details.append(self._delete_url(f"forwarding rule {fname}", url))
        self._finalize(5, "Forwarding Rules", details)

    def step6_vpn_tunnels(self):
        details = []
        for t in self._list(
            f"{self.BASE}/projects/{self.project}/regions/{self.region}/vpnTunnels",
            {"filter": self._avx_filter()},
        ):
            tname = t["name"]
            url   = f"{self.BASE}/projects/{self.project}/regions/{self.region}/vpnTunnels/{tname}"
            details.append(self._delete_url(f"VPN tunnel {tname}", url))
        self._finalize(6, "VPN Tunnels", details)

    def step7_vpn_gateways(self):
        details = []
        for kind, seg in [("classic", "targetVpnGateways"), ("HA", "vpnGateways")]:
            for gw in self._list(
                f"{self.BASE}/projects/{self.project}/regions/{self.region}/{seg}",
                {"filter": self._avx_filter()},
            ):
                gname = gw["name"]
                url   = f"{self.BASE}/projects/{self.project}/regions/{self.region}/{seg}/{gname}"
                details.append(self._delete_url(f"{kind} VPN gateway {gname}", url))
        self._finalize(7, "VPN Gateways", details)

    def step8_routers(self):
        details = []
        for r in self._list(
            f"{self.BASE}/projects/{self.project}/regions/{self.region}/routers",
            {"filter": self._avx_filter()},
        ):
            rname = r["name"]
            url   = f"{self.BASE}/projects/{self.project}/regions/{self.region}/routers/{rname}"
            details.append(self._delete_url(f"router {rname}", url))
        self._finalize(8, "Cloud Routers", details)

    def step9_addresses(self):
        details = []
        for addr in self._list(
            f"{self.BASE}/projects/{self.project}/regions/{self.region}/addresses",
            {"filter": self._avx_filter()},
        ):
            aname = addr["name"]
            url   = f"{self.BASE}/projects/{self.project}/regions/{self.region}/addresses/{aname}"
            details.append(self._delete_url(f"address {aname}", url))
        self._finalize(9, "Static IP Addresses", details)

    def _is_avx(self, name: str) -> bool:
        return name.lower().startswith("avx-")

    def _avx_and_peer_networks(self) -> set:
        """Return names of avx- networks PLUS any network that owns an avx- firewall,
        subnet, or route. Routes are checked last and survive longer than firewalls/subnets,
        so they act as the final breadcrumb when everything else has already been deleted.
        GCP's name: filter is unreliable on many endpoints — always fetch all, filter client-side."""
        nets = set()
        # Networks whose name starts with avx-
        for n in self._list(f"{self.BASE}/projects/{self.project}/global/networks"):
            if "_list_error" in n:
                continue
            if self._is_avx(n.get("name", "")):
                nets.add(n["name"])
        # Networks that own an avx- firewall (e.g. gcp-spoke / gcp-transit)
        for fw in self._list(f"{self.BASE}/projects/{self.project}/global/firewalls"):
            if "_list_error" in fw:
                continue
            if self._is_avx(fw.get("name", "")):
                net_name = fw.get("network", "").rsplit("/", 1)[-1]
                if net_name:
                    nets.add(net_name)
        # Networks that own an avx- subnet in this region
        for sn in self._list(
            f"{self.BASE}/projects/{self.project}/regions/{self.region}/subnetworks"
        ):
            if "_list_error" in sn:
                continue
            if self._is_avx(sn.get("name", "")):
                net_name = sn.get("network", "").rsplit("/", 1)[-1]
                if net_name:
                    nets.add(net_name)
        # Networks that own an avx- route — routes outlast firewalls and subnets,
        # so this catches peer networks (gcp-spoke, gcp-transit) even after prior
        # cleanup runs have already removed everything else.
        for route in self._list(f"{self.BASE}/projects/{self.project}/global/routes"):
            if "_list_error" in route:
                continue
            if self._is_avx(route.get("name", "")):
                net_name = route.get("network", "").rsplit("/", 1)[-1]
                if net_name:
                    nets.add(net_name)
        self._dbg(f"_avx_and_peer_networks raw result: {sorted(nets)}")
        return nets

    def step10_firewall_rules(self, avx_nets: set):
        details = []
        for fw in self._list(f"{self.BASE}/projects/{self.project}/global/firewalls"):
            if "_list_error" in fw:
                details.append(f"error listing firewalls: {fw['_list_error']}")
                continue
            fname    = fw["name"]
            net_name = fw.get("network", "").rsplit("/", 1)[-1]
            if not (self._is_avx(fname) or net_name in avx_nets):
                continue
            url = f"{self.BASE}/projects/{self.project}/global/firewalls/{fname}"
            details.append(self._delete_url(f"firewall {fname}", url))
        self._finalize(10, "Firewall Rules", details)

    def step11_routes(self, avx_nets: set):
        details = []
        for route in self._list(f"{self.BASE}/projects/{self.project}/global/routes"):
            if "_list_error" in route:
                details.append(f"error listing routes: {route['_list_error']}")
                continue
            rname    = route["name"]
            net_name = route.get("network", "").rsplit("/", 1)[-1]
            # Only delete avx- named routes. Default system routes (default-route-*)
            # cannot be deleted directly — GCP removes them automatically when the
            # network is deleted. Deleting by network membership alone would catch
            # those and return a 400 error.
            if not self._is_avx(rname):
                continue
            if net_name not in avx_nets:
                continue
            url = f"{self.BASE}/projects/{self.project}/global/routes/{rname}"
            details.append(self._delete_url(f"route {rname}", url))
        self._finalize(11, "Custom Routes", details)

    def step12_subnets(self, avx_nets: set):
        details = []
        for sn in self._list(
            f"{self.BASE}/projects/{self.project}/regions/{self.region}/subnetworks"
        ):
            if "_list_error" in sn:
                details.append(f"error listing subnets: {sn['_list_error']}")
                continue
            net_name = sn.get("network", "").rsplit("/", 1)[-1]
            sname    = sn["name"]
            if not (self._is_avx(sname) or net_name in avx_nets):
                continue
            url = f"{self.BASE}/projects/{self.project}/regions/{self.region}/subnetworks/{sname}"
            details.append(self._delete_url(f"subnet {sname}", url))
        self._finalize(12, "Subnets", details)

    def step13_networks(self, avx_nets: set):
        details = []
        if not avx_nets:
            self._emit(13, "VPC Networks", "skipped", "no aviatrix networks found")
            return
        for net in sorted(avx_nets):
            url = f"{self.BASE}/projects/{self.project}/global/networks/{net}"
            details.append(self._delete_url(f"network {net}", url))
        self._finalize(13, "VPC Networks", details)

    def step14_snapshots(self):
        details = []
        for snap in self._list(
            f"{self.BASE}/projects/{self.project}/global/snapshots",
            {"filter": self._avx_filter()},
        ):
            sname    = snap["name"]
            src_disk = snap.get("sourceDisk", "")
            if self.region not in src_disk and src_disk:
                continue
            url = f"{self.BASE}/projects/{self.project}/global/snapshots/{sname}"
            details.append(self._delete_url(f"snapshot {sname}", url))
        self._finalize(14, "Snapshots", details)

    def run(self):
        self._dbg(f"Starting GCP cleanup for project={self.project} region={self.region} dry_run={self.dry_run}")
        try:
            self._get(f"{self.BASE}/projects/{self.project}/global/networks")
        except Exception as exc:
            self._emit(1, f"GCP project {self.project}", "error",
                       f"cannot reach project: {exc}")
            self.any_error = True
            return

        # Discover aviatrix networks once before any deletion so the set stays
        # stable even after firewalls/subnets are deleted in later steps.
        avx_nets = self._avx_and_peer_networks()
        self._dbg(f"avx_nets discovered: {sorted(avx_nets)}")

        self.step1_gke()
        self.step2_instances(avx_nets)
        self.step3_disks(avx_nets)
        self.step4_instance_groups(avx_nets)
        self.step5_forwarding_rules()
        self.step6_vpn_tunnels()
        self.step7_vpn_gateways()
        self.step8_routers()
        self.step9_addresses()
        # Steps 10/11/13 touch global GCP resources (firewalls, routes, VPC
        # networks). Only the primary region runs them; others skip to avoid
        # parallel workers racing on the same endpoints.
        if self.is_primary_region:
            self.step10_firewall_rules(avx_nets)
            self.step11_routes(avx_nets)
        else:
            self._emit(10, "Firewall Rules", "skipped", "handled by primary region")
            self._emit(11, "Custom Routes",  "skipped", "handled by primary region")
        self.step12_subnets(avx_nets)
        if self.is_primary_region:
            self.step13_networks(avx_nets)
        else:
            self._emit(13, "VPC Networks", "skipped", "handled by primary region")
        self.step14_snapshots()
