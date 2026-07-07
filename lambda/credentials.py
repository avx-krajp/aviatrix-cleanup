"""
credentials.py — Secrets Manager credential fetchers for Azure and GCP.
Shared by the cleaners (lambda/cleaners/) and the instances routes
(lambda/instances_routes.py) — both need the same SP/SA credentials.
"""

import json
import os
import boto3

REGION_ENV          = os.environ.get("AWS_REGION", "us-east-1")
AZURE_SP_SECRET_ARN = os.environ.get("AZURE_SP_SECRET_ARN", "")
GCP_SA_SECRET_ARN   = os.environ.get("GCP_SA_SECRET_ARN", "")

# Cached credentials, populated on first call
_AZ_CRED_CACHE: tuple | None = None
_GCP_CRED_CACHE: tuple | None = None  # (credentials, project_id)


def azure_sp_raw() -> dict:
    """Fetch the raw Azure SP secret JSON {tenantId, clientId, clientSecret,
    subscriptionId} without importing the azure-identity SDK. Used by
    instances_routes.py, which does its own lightweight OAuth via urllib
    so it doesn't need the Azure SDK layer that only CleanupWorkerFunction has."""
    if not AZURE_SP_SECRET_ARN:
        raise RuntimeError(
            "AZURE_SP_SECRET_ARN env var is empty — Azure instances/cleanup is not "
            "configured. Set the AzureSpSecretArn parameter in template.yaml."
        )
    sm = boto3.client("secretsmanager", region_name=REGION_ENV)
    raw = sm.get_secret_value(SecretId=AZURE_SP_SECRET_ARN)["SecretString"]
    return json.loads(raw)


def azure_credentials():
    """Fetch the Azure SP from Secrets Manager once per cold start; return
    (ClientSecretCredential, subscription_id). Cached on the module."""
    global _AZ_CRED_CACHE
    if _AZ_CRED_CACHE is not None:
        return _AZ_CRED_CACHE
    if not AZURE_SP_SECRET_ARN:
        raise RuntimeError(
            "AZURE_SP_SECRET_ARN env var is empty — Azure cleanup is not configured. "
            "Set the AzureSpSecretArn parameter in template.yaml."
        )
    # Imports kept local so cold start for AWS-only invocations stays cheap.
    from azure.identity import ClientSecretCredential
    sm = boto3.client("secretsmanager", region_name=REGION_ENV)
    raw = sm.get_secret_value(SecretId=AZURE_SP_SECRET_ARN)["SecretString"]
    sp = json.loads(raw)
    cred = ClientSecretCredential(
        tenant_id=sp["tenantId"],
        client_id=sp["clientId"],
        client_secret=sp["clientSecret"],
    )
    _AZ_CRED_CACHE = (cred, sp["subscriptionId"])
    return _AZ_CRED_CACHE


def gcp_credentials():
    """Fetch GCP SA JSON from Secrets Manager once per cold start; return
    (google.oauth2.service_account.Credentials, project_id). Cached on module."""
    global _GCP_CRED_CACHE
    if _GCP_CRED_CACHE is not None:
        return _GCP_CRED_CACHE
    if not GCP_SA_SECRET_ARN:
        raise RuntimeError(
            "GCP_SA_SECRET_ARN env var is empty — GCP cleanup is not configured. "
            "Set the GcpSaSecretArn parameter in template.yaml."
        )
    from google.oauth2 import service_account
    sm = boto3.client("secretsmanager", region_name=REGION_ENV)
    raw = sm.get_secret_value(SecretId=GCP_SA_SECRET_ARN)["SecretString"]
    sa_info = json.loads(raw)
    scopes = [
        "https://www.googleapis.com/auth/cloud-platform",
        "https://www.googleapis.com/auth/compute",
    ]
    cred = service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)
    project_id = sa_info["project_id"]
    _GCP_CRED_CACHE = (cred, project_id)
    return _GCP_CRED_CACHE
