"""
instances_routes.py — instance list/start/stop routes for AWS EC2, Azure VMs,
and GCP Compute instances. Called from cleanup_routes.handler() and gated by
the same cookie authorizer as /api/cleanup — do not expose these unauthenticated.

Routes (wired in template.yaml):
  GET  /api/instances               — list instances (?provider=aws|azure|gcp&region=...)
  POST /api/instances/start         — start one instance
  POST /api/instances/stop          — stop one instance
  POST /api/instances/start-all     — start all stopped instances for a provider
  POST /api/instances/stop-all      — stop all running instances for a provider
"""

import json
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3

from credentials import azure_sp_raw, gcp_credentials
from regions import ALL_AWS_REGIONS

_azure_token_cache = {}


def _resp(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def _params(event: dict) -> dict:
    return event.get("queryStringParameters") or {}


# ═════════════════════════════════════════════════════════════════════════════
# AWS
# ═════════════════════════════════════════════════════════════════════════════

# Aviatrix controller/copilot images — these get an https://<ip> link instead
# of a plain IP in the instances list, since they're accessed via a web UI.
AVX_AMI_PREFIXES = ("avx-controller-", "avx-copilot-")
AVX_AZURE_SKU_PREFIXES = ("aviatrix-controller-", "avx-cplt-byol-")


def _is_avx_ami(name: str) -> bool:
    return any(name.startswith(p) for p in AVX_AMI_PREFIXES)


def _is_avx_azure_sku(sku: str) -> bool:
    return any(sku.startswith(p) for p in AVX_AZURE_SKU_PREFIXES)


def _fetch_region_instances(region: str) -> list:
    client = boto3.client("ec2", region_name=region)
    response = client.describe_instances(
        Filters=[{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]
    )
    reservations = response["Reservations"]

    image_ids = {
        instance["ImageId"]
        for reservation in reservations
        for instance in reservation["Instances"]
        if instance.get("ImageId")
    }
    ami_names = {}
    if image_ids:
        images = client.describe_images(ImageIds=list(image_ids)).get("Images", [])
        ami_names = {img["ImageId"]: img.get("Name", "") for img in images}

    instances = []
    for reservation in reservations:
        for instance in reservation["Instances"]:
            name = next(
                (t["Value"] for t in instance.get("Tags", []) if t["Key"] == "Name"),
                instance["InstanceId"],
            )
            ami = ami_names.get(instance.get("ImageId", ""), "")
            public_ip = instance.get("PublicIpAddress", "")
            instances.append({
                "id":           instance["InstanceId"],
                "name":         name,
                "state":        instance["State"]["Name"],
                "instanceType": instance.get("InstanceType", "unknown"),
                "region":       region,
                "provider":     "AWS",
                "type":         "EC2",
                "publicIp":     public_ip,
                "ami":          ami,
                "linkUrl":      f"https://{public_ip}" if public_ip and _is_avx_ami(ami) else "",
            })
    return instances


def get_aws_instances(event: dict) -> dict:
    region = _params(event).get("region")
    try:
        if region:
            instances = _fetch_region_instances(region)
        else:
            instances = []
            with ThreadPoolExecutor(max_workers=len(ALL_AWS_REGIONS)) as ex:
                futures = {ex.submit(_fetch_region_instances, r): r for r in ALL_AWS_REGIONS}
                for future in as_completed(futures):
                    try:
                        instances.extend(future.result())
                    except Exception:
                        pass
        return _resp(200, {"status": "success", "count": len(instances), "instances": instances})
    except Exception as exc:
        return _resp(500, {"status": "error", "message": str(exc)})


def start_aws_instance(event: dict) -> dict:
    p = _params(event)
    instance_id = p.get("instanceId", "")
    region = p.get("region", "us-east-1")
    try:
        boto3.client("ec2", region_name=region).start_instances(InstanceIds=[instance_id])
        return _resp(200, {"status": "success", "message": f"Starting {instance_id}"})
    except Exception as exc:
        return _resp(500, {"status": "error", "message": str(exc)})


def stop_aws_instance(event: dict) -> dict:
    p = _params(event)
    instance_id = p.get("instanceId", "")
    region = p.get("region", "us-east-1")
    try:
        boto3.client("ec2", region_name=region).stop_instances(InstanceIds=[instance_id])
        return _resp(200, {"status": "success", "message": f"Stopping {instance_id}"})
    except Exception as exc:
        return _resp(500, {"status": "error", "message": str(exc)})


def start_all_aws_instances(event: dict) -> dict:
    p = _params(event)
    ids = [i for i in p.get("instanceIds", "").split(",") if i]
    region = p.get("region", "us-east-1")
    try:
        if ids:
            boto3.client("ec2", region_name=region).start_instances(InstanceIds=ids)
        return _resp(200, {"status": "success", "message": f"Starting {len(ids)}"})
    except Exception as exc:
        return _resp(500, {"status": "error", "message": str(exc)})


def stop_all_aws_instances(event: dict) -> dict:
    p = _params(event)
    ids = [i for i in p.get("instanceIds", "").split(",") if i]
    region = p.get("region", "us-east-1")
    try:
        if ids:
            boto3.client("ec2", region_name=region).stop_instances(InstanceIds=ids)
        return _resp(200, {"status": "success", "message": f"Stopping {len(ids)}"})
    except Exception as exc:
        return _resp(500, {"status": "error", "message": str(exc)})


# ═════════════════════════════════════════════════════════════════════════════
# Azure
# ═════════════════════════════════════════════════════════════════════════════

def _get_azure_token() -> str:
    """Raw OAuth client-credentials grant via urllib — avoids depending on the
    azure-identity SDK (which lives in a layer only CleanupWorkerFunction has)."""
    now = time.time()
    if _azure_token_cache.get("token") and now < _azure_token_cache.get("expiry", 0):
        return _azure_token_cache["token"]

    sp = azure_sp_raw()
    token_url = f"https://login.microsoftonline.com/{sp['tenantId']}/oauth2/token"
    data = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     sp["clientId"],
        "client_secret": sp["clientSecret"],
        "resource":      "https://management.azure.com/",
    }).encode()
    req = urllib.request.Request(
        token_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read())

    token = result.get("access_token", "")
    expires_in = int(result.get("expires_in", 3600))
    _azure_token_cache["token"] = token
    _azure_token_cache["expiry"] = now + expires_in - 300
    return token


def _azure_api_call(url: str, method: str = "GET", body: dict = None):
    try:
        token = _get_azure_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as response:
            resp_body = response.read()
            return (json.loads(resp_body) if resp_body else {}), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}: {exc.read().decode()}"
    except Exception as exc:
        return None, str(exc)


def _map_azure_state(power_state: str) -> str:
    return {
        "running": "running", "stopped": "stopped", "deallocated": "stopped",
        "starting": "starting", "stopping": "stopping",
    }.get(power_state.lower(), "unknown")


def _azure_vm_public_ip(vm: dict) -> str:
    """Resolve a VM's first public IP via its NIC — a Network API round-trip
    only worth paying for on the (rare) controller/copilot matches, not every
    VM in the list."""
    nics = vm.get("properties", {}).get("networkProfile", {}).get("networkInterfaces", [])
    if not nics:
        return ""
    nic_id = nics[0].get("id", "")
    if not nic_id:
        return ""
    nic_result, error = _azure_api_call(
        f"https://management.azure.com{nic_id}?api-version=2023-05-01"
    )
    if error:
        return ""
    ip_configs = nic_result.get("properties", {}).get("ipConfigurations", [])
    if not ip_configs:
        return ""
    pip_ref = ip_configs[0].get("properties", {}).get("publicIPAddress", {})
    pip_id = pip_ref.get("id", "")
    if not pip_id:
        return ""
    pip_result, error = _azure_api_call(
        f"https://management.azure.com{pip_id}?api-version=2023-05-01"
    )
    if error:
        return ""
    return pip_result.get("properties", {}).get("ipAddress", "") or ""


def get_azure_instances(event: dict) -> dict:
    region = _params(event).get("region")
    try:
        subscription_id = azure_sp_raw()["subscriptionId"]
        url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            f"/providers/Microsoft.Compute/virtualMachines"
            f"?api-version=2023-03-01&$expand=instanceView"
        )
        result, error = _azure_api_call(url)
        if error:
            return _resp(500, {"status": "error", "message": error})

        vms = []
        for vm in result.get("value", []):
            vm_id = vm.get("id", "")
            vm_location = vm.get("location", "")
            resource_group = (
                vm_id.split("/resourceGroups/")[1].split("/")[0]
                if "/resourceGroups/" in vm_id else ""
            )
            if region and vm_location.lower() != region.lower():
                continue

            power_state = "unknown"
            instance_view = vm.get("properties", {}).get("instanceView", {})
            for status in instance_view.get("statuses", []):
                code = status.get("code", "")
                if code.startswith("PowerState/"):
                    power_state = code.replace("PowerState/", "")
                    break

            sku = (
                vm.get("plan", {}).get("name", "")
                or vm.get("properties", {}).get("storageProfile", {})
                       .get("imageReference", {}).get("sku", "")
            )
            link_url = ""
            if _is_avx_azure_sku(sku):
                public_ip = _azure_vm_public_ip(vm)
                if public_ip:
                    link_url = f"https://{public_ip}"

            vms.append({
                "id":            vm_id,
                "name":          vm.get("name", ""),
                "state":         _map_azure_state(power_state),
                "instanceType":  vm.get("properties", {}).get("hardwareProfile", {}).get("vmSize", "unknown"),
                "region":        vm_location,
                "resourceGroup": resource_group,
                "provider":      "AZURE",
                "type":          "VIRTUAL_MACHINE",
                "sku":           sku,
                "linkUrl":       link_url,
            })
        return _resp(200, {"status": "success", "count": len(vms), "instances": vms, "provider": "AZURE"})
    except Exception as exc:
        return _resp(500, {"status": "error", "message": str(exc)})


def start_azure_vm(event: dict) -> dict:
    p = _params(event)
    vm_name = p.get("vmName", "")
    resource_group = p.get("resourceGroup", "")
    try:
        subscription_id = azure_sp_raw()["subscriptionId"]
        url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            f"/resourceGroups/{resource_group}/providers/Microsoft.Compute"
            f"/virtualMachines/{vm_name}/start?api-version=2023-03-01"
        )
        _, error = _azure_api_call(url, method="POST", body={})
        if error and "202" not in str(error):
            return _resp(500, {"status": "error", "message": error})
        return _resp(200, {"status": "success", "message": f"Starting {vm_name}"})
    except Exception as exc:
        return _resp(500, {"status": "error", "message": str(exc)})


def stop_azure_vm(event: dict) -> dict:
    p = _params(event)
    vm_name = p.get("vmName", "")
    resource_group = p.get("resourceGroup", "")
    try:
        subscription_id = azure_sp_raw()["subscriptionId"]
        url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            f"/resourceGroups/{resource_group}/providers/Microsoft.Compute"
            f"/virtualMachines/{vm_name}/deallocate?api-version=2023-03-01"
        )
        _, error = _azure_api_call(url, method="POST", body={})
        if error and "202" not in str(error):
            return _resp(500, {"status": "error", "message": error})
        return _resp(200, {"status": "success", "message": f"Stopping {vm_name}"})
    except Exception as exc:
        return _resp(500, {"status": "error", "message": str(exc)})


# ═════════════════════════════════════════════════════════════════════════════
# GCP
# ═════════════════════════════════════════════════════════════════════════════

def _map_gcp_state(status: str) -> str:
    return {
        "RUNNING": "running", "TERMINATED": "stopped", "STOPPING": "stopping",
        "STAGING": "starting", "PROVISIONING": "starting", "SUSPENDED": "stopped",
        "REPAIRING": "unknown",
    }.get(status.upper(), "unknown")


def _gcp_api_call(url: str, method: str = "GET", body: dict = None):
    try:
        credentials, _ = gcp_credentials()
        credentials.refresh(_GoogleAuthRequest())
        headers = {"Authorization": f"Bearer {credentials.token}", "Content-Type": "application/json"}
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as response:
            resp_body = response.read()
            return (json.loads(resp_body) if resp_body else {}), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}: {exc.read().decode()}"
    except Exception as exc:
        return None, str(exc)


def _GoogleAuthRequest():
    from google.auth.transport.requests import Request
    return Request()


def _parse_gcp_instance(item: dict, zone: str) -> dict:
    return {
        "id":           str(item.get("id", "")),
        "name":         item.get("name", ""),
        "state":        _map_gcp_state(item.get("status", "UNKNOWN")),
        "instanceType": item.get("machineType", "").split("/")[-1],
        "region":       zone,
        "provider":     "GCP",
        "type":         "COMPUTE_ENGINE",
    }


def get_gcp_instances(event: dict) -> dict:
    zone = _params(event).get("zone")
    try:
        _, project_id = gcp_credentials()
        instances = []
        if zone:
            url = f"https://compute.googleapis.com/compute/v1/projects/{project_id}/zones/{zone}/instances"
            result, error = _gcp_api_call(url)
            if error:
                return _resp(500, {"status": "error", "message": error})
            for item in result.get("items", []):
                instances.append(_parse_gcp_instance(item, zone))
        else:
            url = (
                f"https://compute.googleapis.com/compute/v1/projects/{project_id}"
                f"/aggregated/instances?maxResults=500"
            )
            result, error = _gcp_api_call(url)
            if error:
                return _resp(500, {"status": "error", "message": error})
            for zone_key, zone_data in result.get("items", {}).items():
                zone_name = zone_key.replace("zones/", "")
                for item in zone_data.get("instances", []):
                    instances.append(_parse_gcp_instance(item, zone_name))
        return _resp(200, {"status": "success", "count": len(instances), "instances": instances, "provider": "GCP"})
    except Exception as exc:
        return _resp(500, {"status": "error", "message": str(exc)})


def start_gcp_instance(event: dict) -> dict:
    p = _params(event)
    vm_name = p.get("vmName", "")
    zone = p.get("zone", "us-east1-c")
    try:
        _, project_id = gcp_credentials()
        url = f"https://compute.googleapis.com/compute/v1/projects/{project_id}/zones/{zone}/instances/{vm_name}/start"
        _, error = _gcp_api_call(url, method="POST", body={})
        if error and "200" not in str(error):
            return _resp(500, {"status": "error", "message": error})
        return _resp(200, {"status": "success", "message": f"Starting {vm_name}"})
    except Exception as exc:
        return _resp(500, {"status": "error", "message": str(exc)})


def stop_gcp_instance(event: dict) -> dict:
    p = _params(event)
    vm_name = p.get("vmName", "")
    zone = p.get("zone", "us-east1-c")
    try:
        _, project_id = gcp_credentials()
        url = f"https://compute.googleapis.com/compute/v1/projects/{project_id}/zones/{zone}/instances/{vm_name}/stop"
        _, error = _gcp_api_call(url, method="POST", body={})
        if error and "200" not in str(error):
            return _resp(500, {"status": "error", "message": error})
        return _resp(200, {"status": "success", "message": f"Stopping {vm_name}"})
    except Exception as exc:
        return _resp(500, {"status": "error", "message": str(exc)})


# ═════════════════════════════════════════════════════════════════════════════
# Direct (non-HTTP) helpers for schedule_runner.py — same Lambda role, no
# cookie auth available for an internal HTTP round-trip, so call these
# functions directly instead of going through API Gateway.
# ═════════════════════════════════════════════════════════════════════════════

def list_running_aws() -> list:
    instances = []
    for r in ALL_AWS_REGIONS:
        try:
            instances.extend(_fetch_region_instances(r))
        except Exception:
            pass
    return [i for i in instances if i["state"].lower() == "running"]


def stop_aws_by_ids(ids: list, region: str) -> int:
    if not ids:
        return 0
    boto3.client("ec2", region_name=region).stop_instances(InstanceIds=ids)
    return len(ids)


def list_running_azure() -> list:
    result = get_azure_instances({"queryStringParameters": {}})
    body = json.loads(result["body"])
    return [vm for vm in body.get("instances", []) if vm["state"] == "running"]


def stop_azure_by_name(vm_name: str, resource_group: str) -> bool:
    result = stop_azure_vm({"queryStringParameters": {
        "vmName": vm_name, "resourceGroup": resource_group,
    }})
    return json.loads(result["body"]).get("status") == "success"


def list_running_gcp() -> list:
    result = get_gcp_instances({"queryStringParameters": {}})
    body = json.loads(result["body"])
    return [vm for vm in body.get("instances", []) if vm["state"] == "running"]


def stop_gcp_by_name(vm_name: str, zone: str) -> bool:
    result = stop_gcp_instance({"queryStringParameters": {
        "vmName": vm_name, "zone": zone,
    }})
    return json.loads(result["body"]).get("status") == "success"


# ═════════════════════════════════════════════════════════════════════════════
# Dispatcher — called from cleanup_routes.handler() for /api/instances* routes.
# Provider is passed as a query-string param (?provider=aws|azure|gcp) rather
# than baked into the path, so all providers share one set of routes.
# ═════════════════════════════════════════════════════════════════════════════

_LIST_ROUTES = {
    "aws":   get_aws_instances,
    "azure": get_azure_instances,
    "gcp":   get_gcp_instances,
}
_START_ROUTES = {
    "aws":   start_aws_instance,
    "azure": start_azure_vm,
    "gcp":   start_gcp_instance,
}
_STOP_ROUTES = {
    "aws":   stop_aws_instance,
    "azure": stop_azure_vm,
    "gcp":   stop_gcp_instance,
}
_START_ALL_ROUTES = {
    "aws": start_all_aws_instances,
}
_STOP_ALL_ROUTES = {
    "aws": stop_all_aws_instances,
}


def route(action: str, event: dict) -> dict:
    """action is one of: list, start, stop, start-all, stop-all.
    Provider comes from ?provider=aws|azure|gcp in the query string."""
    provider = _params(event).get("provider", "").lower()
    table = {
        "list":      _LIST_ROUTES,
        "start":     _START_ROUTES,
        "stop":      _STOP_ROUTES,
        "start-all": _START_ALL_ROUTES,
        "stop-all":  _STOP_ALL_ROUTES,
    }.get(action)
    if table is None:
        return _resp(404, {"status": "error", "message": f"Unknown instances action '{action}'"})
    fn = table.get(provider)
    if fn is None:
        return _resp(400, {"status": "error", "message": f"provider must be one of {list(table.keys())}"})
    return fn(event)
