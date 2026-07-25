"""
cleanup_routes.py — API Gateway Lambda handler
Routes:
  POST /api/cleanup          — start a cleanup job (async)
  GET  /api/cleanup/status   — poll job status by jobId
  GET  /api/schedule         — read current daily-stop schedule
  PUT  /api/schedule         — set/update the schedule
  POST /api/schedule/skip    — skip today's run
  POST /api/schedule/unskip  — un-skip today's run
"""

import json
import os
import uuid
import boto3
from datetime import datetime, timedelta, timezone

from regions import ALL_AWS_REGIONS, ALL_AZURE_REGIONS, ALL_GCP_REGIONS
import instances_routes

REGION              = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME          = os.environ.get("CLEANUP_TABLE", "aviatrix-cleanup-jobs")
WORKER_ARN          = os.environ.get("CLEANUP_WORKER_ARN", "")
SCHEDULE_TABLE_NAME = os.environ.get("SCHEDULE_TABLE", "aviatrix-cleanup-schedule")
STOP_RUNNER_ARN     = os.environ.get("STOP_RUNNER_ARN", "")
SCHEDULER_ROLE_ARN  = os.environ.get("SCHEDULER_ROLE_ARN", "")
SCHEDULE_NAME       = os.environ.get("SCHEDULE_NAME", "aviatrix-cleanup-stop-daily")
SCHEDULE_KEY        = "default"
IST_OFFSET_MIN      = 5 * 60 + 30
IST                 = timezone(timedelta(minutes=IST_OFFSET_MIN))

dynamodb  = boto3.resource("dynamodb", region_name=REGION)
lambda_   = boto3.client("lambda",   region_name=REGION)
scheduler = boto3.client("scheduler", region_name=REGION)


# ── helpers ───────────────────────────────────────────────────────────────────

def _resp(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def _table():
    return dynamodb.Table(TABLE_NAME)


# ── POST /cleanup ─────────────────────────────────────────────────────────────

def _fire_worker(job_id: str, cloud: str, region: str, vpc_id: str, dry_run: bool,
                  is_primary_region: bool = False):
    payload = json.dumps({
        "job_id":             job_id,
        "cloud":              cloud,
        "region":             region,
        "vpc_id":             vpc_id,
        "dry_run":            dry_run,
        "is_primary_region":  is_primary_region,
    })
    lambda_.invoke(
        FunctionName   = WORKER_ARN,
        InvocationType = "Event",
        Payload        = payload.encode(),
    )


def start_cleanup(event: dict) -> dict:
    """
    Body (JSON):
      cloud       aws | azure                    (required)
      region      AWS region or "all-regions"    (required)
      vpc_id      optional VPC filter
      dry_run     bool, default false
    """
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "Invalid JSON body"})

    cloud   = body.get("cloud", "").strip().lower()
    region  = body.get("region", "").strip()
    vpc_id  = body.get("vpc_id", "")
    dry_run = bool(body.get("dry_run", False))

    if cloud not in ("aws", "azure", "gcp"):
        return _resp(400, {"error": "cloud must be 'aws', 'azure', or 'gcp'"})
    if not region:
        return _resp(400, {"error": "region is required"})
    if not WORKER_ARN:
        return _resp(500, {"error": "CLEANUP_WORKER_ARN env var not configured"})

    now_iso = datetime.now(timezone.utc).isoformat()

    # ── All-regions fan-out (AWS + Azure) ────────────────────────────────────
    if region == "all-regions":
        parent_id    = str(uuid.uuid4())
        child_ids    = []

        if cloud == "aws":
            regions = ALL_AWS_REGIONS
        elif cloud == "azure":
            regions = ALL_AZURE_REGIONS
        else:
            regions = ALL_GCP_REGIONS

        for idx, r in enumerate(regions):
            child_id = str(uuid.uuid4())
            child_ids.append(child_id)
            _table().put_item(Item={
                "jobId":     child_id,
                "cloud":     cloud,
                "region":    r,
                "vpcId":     "",
                "dryRun":    dry_run,
                "status":    "PENDING",
                "steps":     [],
                "createdAt": now_iso,
                "updatedAt": now_iso,
            })
            # For GCP, only the first region worker handles global resources
            # (firewalls, routes, VPC networks) to prevent parallel workers
            # from racing each other on the same global GCP API endpoints.
            is_primary = (cloud == "gcp" and idx == 0)
            _fire_worker(child_id, cloud, r, "", dry_run, is_primary_region=is_primary)

        _table().put_item(Item={
            "jobId":       parent_id,
            "cloud":       cloud,
            "region":      "all-regions",
            "vpcId":       "",
            "dryRun":      dry_run,
            "status":      "RUNNING",
            "steps":       [],
            "childJobIds": child_ids,
            "createdAt":   now_iso,
            "updatedAt":   now_iso,
        })

        return _resp(202, {
            "jobId":       parent_id,
            "status":      "RUNNING",
            "childJobIds": child_ids,
            "message":     f"All-regions cleanup started: {len(regions)} {cloud.upper()} regions in parallel",
        })

    # ── Single-region (existing flow) ─────────────────────────────────────────
    job_id = str(uuid.uuid4())
    _table().put_item(Item={
        "jobId":     job_id,
        "cloud":     cloud,
        "region":    region,
        "vpcId":     vpc_id,
        "dryRun":    dry_run,
        "status":    "PENDING",
        "steps":     [],
        "createdAt": now_iso,
        "updatedAt": now_iso,
    })
    _fire_worker(job_id, cloud, region, vpc_id, dry_run)

    return _resp(202, {
        "jobId":  job_id,
        "status": "PENDING",
        "message": f"Cleanup job started for {cloud.upper()} / {region}",
    })


# ── GET /cleanup/status ───────────────────────────────────────────────────────

def get_status(event: dict) -> dict:
    """
    Query string: jobId=<uuid>
    For a parent all-regions job, aggregates child job statuses.
    """
    params = event.get("queryStringParameters") or {}
    job_id = params.get("jobId", "").strip()

    if not job_id:
        return _resp(400, {"error": "jobId query parameter is required"})

    result = _table().get_item(Key={"jobId": job_id})
    item   = result.get("Item")

    if not item:
        return _resp(404, {"error": f"Job '{job_id}' not found"})

    item = _decimal_fix(item)

    # ── Aggregate child jobs for all-regions parent ───────────────────────────
    child_ids = item.get("childJobIds", [])
    if child_ids:
        children = []
        # batch_get_item caps at 100 keys per call — all-regions fan-out never
        # exceeds that (largest is Azure's ~45 regions), but chunk defensively.
        for i in range(0, len(child_ids), 100):
            chunk = child_ids[i:i + 100]
            resp = dynamodb.batch_get_item(RequestItems={
                TABLE_NAME: {"Keys": [{"jobId": cid} for cid in chunk]}
            })
            children.extend(resp.get("Responses", {}).get(TABLE_NAME, []))
        children = [_decimal_fix(c) for c in children]
        # batch_get_item does not preserve key order — restore childJobIds order
        # so the UI's per-region cards stay in a stable position across polls.
        order = {cid: idx for idx, cid in enumerate(child_ids)}
        children.sort(key=lambda c: order.get(c["jobId"], len(child_ids)))

        # Derive parent status from children.
        # Guard against empty children list (DDB eventual-consistency miss) —
        # all([]) is vacuously True in Python and would incorrectly COMPLETE the job.
        # For all-regions jobs, partial errors are expected and shown per-region
        # in the UI — mark parent COMPLETE once all children have finished.
        statuses = [c["status"] for c in children]
        if statuses and all(s in ("COMPLETE", "ERROR") for s in statuses):
            parent_status = "COMPLETE"
        else:
            parent_status = "RUNNING"

        # Update parent status in DDB if it changed
        if parent_status != item["status"]:
            _table().update_item(
                Key={"jobId": job_id},
                UpdateExpression="SET #s = :s, updatedAt = :u",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": parent_status,
                    ":u": datetime.now(timezone.utc).isoformat(),
                },
            )
            item["status"] = parent_status

        item["children"] = children

    return _resp(200, item)


# ── Schedule routes ───────────────────────────────────────────────────────────

def _schedule_table():
    return dynamodb.Table(SCHEDULE_TABLE_NAME)


def _today_ist_date() -> str:
    return datetime.now(IST).date().isoformat()


def _ist_to_utc_cron(time_hhmm: str) -> str:
    h, m = map(int, time_hhmm.split(":"))
    total = (h * 60 + m - IST_OFFSET_MIN) % (24 * 60)
    uh, um = divmod(total, 60)
    return f"cron({um} {uh} * * ? *)"


def get_schedule(_event):
    item = _schedule_table().get_item(Key={"id": SCHEDULE_KEY}).get("Item") or {}
    today = _today_ist_date()
    return _resp(200, {
        "time":           item.get("time"),
        "tz":             "Asia/Kolkata",
        "skipDate":       item.get("skipDate"),
        "todayDate":      today,
        "isSkippedToday": item.get("skipDate") == today,
    })


def put_schedule(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "Invalid JSON body"})

    time_str = (body.get("time") or "").strip()
    try:
        h, m = time_str.split(":")
        h, m = int(h), int(m)
        assert 0 <= h < 24 and 0 <= m < 60
    except (ValueError, AssertionError):
        return _resp(400, {"error": "time must be HH:MM (24-hour)"})

    cron_expr = _ist_to_utc_cron(f"{h:02d}:{m:02d}")
    target = {
        "Arn": STOP_RUNNER_ARN,
        "RoleArn": SCHEDULER_ROLE_ARN,
        "Input": json.dumps({"source": "scheduled"}),
    }

    try:
        scheduler.update_schedule(
            Name               = SCHEDULE_NAME,
            ScheduleExpression = cron_expr,
            FlexibleTimeWindow = {"Mode": "OFF"},
            Target             = target,
            State              = "ENABLED",
        )
    except scheduler.exceptions.ResourceNotFoundException:
        scheduler.create_schedule(
            Name               = SCHEDULE_NAME,
            ScheduleExpression = cron_expr,
            FlexibleTimeWindow = {"Mode": "OFF"},
            Target             = target,
            State              = "ENABLED",
        )

    _schedule_table().put_item(Item={
        "id":        SCHEDULE_KEY,
        "time":      f"{h:02d}:{m:02d}",
        "tz":        "Asia/Kolkata",
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    })

    return _resp(200, {"time": f"{h:02d}:{m:02d}", "tz": "Asia/Kolkata"})


def skip_schedule(_event):
    today = _today_ist_date()
    _schedule_table().update_item(
        Key={"id": SCHEDULE_KEY},
        UpdateExpression="SET skipDate = :d, updatedAt = :u",
        ExpressionAttributeValues={
            ":d": today,
            ":u": datetime.now(timezone.utc).isoformat(),
        },
    )
    return _resp(200, {"skipDate": today})


def unskip_schedule(_event):
    _schedule_table().update_item(
        Key={"id": SCHEDULE_KEY},
        UpdateExpression="REMOVE skipDate SET updatedAt = :u",
        ExpressionAttributeValues={
            ":u": datetime.now(timezone.utc).isoformat(),
        },
    )
    return _resp(200, {"skipDate": None})


def _decimal_fix(obj):
    from decimal import Decimal
    if isinstance(obj, dict):
        return {k: _decimal_fix(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_fix(v) for v in obj]
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


# ── Lambda entrypoint ─────────────────────────────────────────────────────────

def handler(event: dict, _context) -> dict:
    # HTTP API v2 exposes a normalized routeKey like "POST /cleanup" that omits
    # the stage; prefer it. Fall back to method+path (stripping the stage if
    # it's prefixed onto the path, as happens with named stages).
    route_key = event.get("routeKey", "")
    if route_key == "POST /api/cleanup":
        return start_cleanup(event)
    if route_key.startswith("GET /api/cleanup/status"):
        return get_status(event)
    if route_key == "GET /api/schedule":
        return get_schedule(event)
    if route_key == "PUT /api/schedule":
        return put_schedule(event)
    if route_key == "POST /api/schedule/skip":
        return skip_schedule(event)
    if route_key == "POST /api/schedule/unskip":
        return unskip_schedule(event)
    if route_key == "GET /api/instances":
        return instances_routes.route("list", event)
    if route_key == "POST /api/instances/start":
        return instances_routes.route("start", event)
    if route_key == "POST /api/instances/stop":
        return instances_routes.route("stop", event)
    if route_key == "POST /api/instances/start-all":
        return instances_routes.route("start-all", event)
    if route_key == "POST /api/instances/stop-all":
        return instances_routes.route("stop-all", event)

    http_ctx = (event.get("requestContext") or {}).get("http") or {}
    method = event.get("httpMethod") or http_ctx.get("method", "")
    path   = event.get("path") or event.get("rawPath") or http_ctx.get("path", "")
    stage  = (event.get("requestContext") or {}).get("stage", "")
    if stage and path.startswith(f"/{stage}/"):
        path = path[len(stage) + 1:]
    p = path.rstrip("/")

    if method == "POST" and p == "/api/cleanup":
        return start_cleanup(event)
    if method == "GET" and "/api/cleanup/status" in path:
        return get_status(event)
    if method == "GET" and p == "/api/schedule":
        return get_schedule(event)
    if method == "PUT" and p == "/api/schedule":
        return put_schedule(event)
    if method == "POST" and p == "/api/schedule/skip":
        return skip_schedule(event)
    if method == "POST" and p == "/api/schedule/unskip":
        return unskip_schedule(event)
    if method == "GET" and p == "/api/instances":
        return instances_routes.route("list", event)
    if method == "POST" and p == "/api/instances/start":
        return instances_routes.route("start", event)
    if method == "POST" and p == "/api/instances/stop":
        return instances_routes.route("stop", event)
    if method == "POST" and p == "/api/instances/start-all":
        return instances_routes.route("start-all", event)
    if method == "POST" and p == "/api/instances/stop-all":
        return instances_routes.route("stop-all", event)

    return _resp(404, {"error": f"No route for {method} {path}"})
