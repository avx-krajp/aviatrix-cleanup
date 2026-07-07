"""
job_store.py — DynamoDB helpers for cleanup job status tracking.
Shared by cleanup_worker.py and every cleaner in lambda/cleaners/.
"""

import os
import boto3
from datetime import datetime, timezone

REGION_ENV = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME = os.environ.get("CLEANUP_TABLE", "aviatrix-cleanup-jobs")


def ddb(job_region: str = None):
    return boto3.resource("dynamodb", region_name=REGION_ENV).Table(TABLE_NAME)


def update_job(table, job_id: str, status: str, step: dict | None = None):
    now = datetime.now(timezone.utc).isoformat()
    update_expr = "SET #s = :s, updatedAt = :u"
    expr_names  = {"#s": "status"}
    expr_values = {":s": status, ":u": now}

    if step:
        update_expr += ", steps = list_append(if_not_exists(steps, :empty), :step)"
        expr_values[":empty"] = []
        expr_values[":step"]  = [step]

    table.update_item(
        Key={"jobId": job_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def step_record(number: int, total: int, name: str, state: str, detail: str = "") -> dict:
    return {
        "number": number,
        "total":  total,
        "name":   name,
        "state":  state,      # running | done | skipped | error
        "detail": detail,
        "ts":     datetime.now(timezone.utc).isoformat(),
    }
