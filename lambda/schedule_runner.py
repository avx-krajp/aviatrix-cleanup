"""
schedule_runner.py — EventBridge Scheduler target.

Reads the schedule row from DynamoDB. If today's IST date is marked as
skipped, exits without doing anything. Otherwise stops all running
instances across AWS / Azure / GCP by calling instances_routes.py logic
directly (this Lambda has no cookie to authenticate an HTTP call through
API Gateway, and it already shares a role with the routes function).
"""

import os
from datetime import datetime, timedelta, timezone

import boto3

import instances_routes

REGION         = os.environ.get("AWS_REGION", "us-east-1")
SCHEDULE_TABLE = os.environ["SCHEDULE_TABLE"]
SCHEDULE_KEY   = "default"

IST = timezone(timedelta(hours=5, minutes=30))

ddb = boto3.resource("dynamodb", region_name=REGION)


def _today_ist_date() -> str:
    return datetime.now(IST).date().isoformat()


def _stop_aws() -> int:
    running = instances_routes.list_running_aws()
    by_region: dict[str, list[str]] = {}
    for i in running:
        by_region.setdefault(i.get("region") or "us-east-1", []).append(i["id"])
    n = 0
    for region, ids in by_region.items():
        n += instances_routes.stop_aws_by_ids(ids, region)
    return n


def _stop_azure() -> int:
    n = 0
    for vm in instances_routes.list_running_azure():
        if instances_routes.stop_azure_by_name(vm["name"], vm.get("resourceGroup", "")):
            n += 1
    return n


def _stop_gcp() -> int:
    n = 0
    for vm in instances_routes.list_running_gcp():
        if instances_routes.stop_gcp_by_name(vm["name"], vm["region"]):
            n += 1
    return n


def handler(event, _context):
    today = _today_ist_date()
    item = ddb.Table(SCHEDULE_TABLE).get_item(Key={"id": SCHEDULE_KEY}).get("Item") or {}

    if item.get("skipDate") == today:
        print(f"Schedule skipped for {today} IST — nothing to do")
        return {"skipped": True, "date": today}

    aws_n   = _stop_aws()
    azure_n = _stop_azure()
    gcp_n   = _stop_gcp()
    total   = aws_n + azure_n + gcp_n
    print(f"Stopped {total} instances (AWS={aws_n} Azure={azure_n} GCP={gcp_n})")
    return {"stopped": total, "aws": aws_n, "azure": azure_n, "gcp": gcp_n, "date": today}
