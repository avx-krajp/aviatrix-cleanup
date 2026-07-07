"""
cleanup_worker.py — async Lambda worker
Invoked by cleanup_routes.py with InvocationType='Event'.

Payload:
  job_id   str
  cloud    aws | azure | gcp
  region   str
  vpc_id   str   (optional)
  dry_run  bool
"""

from credentials import azure_credentials, gcp_credentials
from job_store import ddb, update_job, step_record
from cleaners import CLEANER_REGISTRY
from cleaners.aws import AWSCleaner
from cleaners.azure import AzureCleaner
from cleaners.gcp import GCPCleaner


def _build_cleaner(cloud: str, region: str, vpc_id: str, dry_run: bool,
                    table, job_id: str, is_primary_region: bool):
    if cloud == "aws":
        return AWSCleaner(
            region=region, vpc_id=vpc_id, dry_run=dry_run,
            table=table, job_id=job_id,
        )
    if cloud == "azure":
        credential, subscription_id = azure_credentials()
        return AzureCleaner(
            region=region, dry_run=dry_run, table=table, job_id=job_id,
            credential=credential, subscription_id=subscription_id,
        )
    if cloud == "gcp":
        gcp_cred, project_id = gcp_credentials()
        return GCPCleaner(
            region=region, dry_run=dry_run, table=table, job_id=job_id,
            credentials=gcp_cred, project_id=project_id,
            is_primary_region=is_primary_region,
        )
    raise ValueError(f"Cloud '{cloud}' not yet supported in worker")


def handler(event: dict, _context):
    job_id  = event["job_id"]
    cloud   = event["cloud"]
    region  = event["region"]
    vpc_id  = event.get("vpc_id", "")
    dry_run = bool(event.get("dry_run", False))

    is_primary_region = bool(event.get("is_primary_region", False))
    table = ddb(region)
    update_job(table, job_id, "RUNNING")

    try:
        if cloud not in CLEANER_REGISTRY:
            update_job(table, job_id, "ERROR",
                       step_record(1, 1, cloud, "error",
                                   f"Cloud '{cloud}' not yet supported in worker"))
            return

        cleaner = _build_cleaner(cloud, region, vpc_id, dry_run, table, job_id,
                                  is_primary_region)
        cleaner.run()
        final_status = "ERROR" if cleaner.any_error else "COMPLETE"
        update_job(table, job_id, final_status)

    except Exception as exc:
        update_job(table, job_id, "ERROR",
                   step_record(0, 0, "unhandled", "error", str(exc)))
        raise
