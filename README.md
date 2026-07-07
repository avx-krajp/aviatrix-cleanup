# Aviatrix Cloud Cleanup

A serverless tool that finds and deletes leftover Aviatrix-created resources
across AWS, Azure, and GCP — for lab/test accounts where controllers, spoke
VPCs/VNets/VPCs, and their dependent resources pile up over time.

Deploys entirely into your own AWS account via SAM: a small web UI (served
from CloudFront + S3), backed by an API Gateway + Lambda stack that fans out
region-by-region cleanup jobs and tracks progress in DynamoDB.

## Architecture

```
Browser
  │
  ▼
CloudFront  ──── static files ────►  S3 (web/)
  │
  └── /api/* ───────────────────►  API Gateway (HTTP API)
                                       │
                    ┌──────────────────┼───────────────────┐
                    ▼                  ▼                   ▼
            auth_login.py    auth_authorizer.py     cleanup_routes.py
            (login/logout)   (cookie auth check)    (/api/cleanup*,
                                                       /api/schedule*,
                                                       /api/instances*)
                                                            │
                                        ┌───────────────────┼──────────────┐
                                        ▼                   ▼              ▼
                              cleanup_worker.py    DynamoDB (jobs,   EventBridge
                              (async, per-region     schedule)       Scheduler
                               cleanup job)                          (daily stop)
                                        │
                              lambda/cleaners/
                              ├── aws.py    (28 steps)
                              ├── azure.py  (26 steps)
                              └── gcp.py    (14 steps)
```

- **Web UI** (`web/`) — Alpine.js single-page app. Pick a cloud + region (or
  "all-regions"), optionally scope to one VPC/RG, dry-run or delete for real.
  Also has an instance dashboard (list/start/stop VMs) and a daily
  auto-stop schedule.
- **`cleanup_routes.py`** — synchronous API handler. Starts jobs, reports
  status, manages the daily-stop schedule, and proxies instance list/start/
  stop calls.
- **`cleanup_worker.py`** — async worker, invoked per-region (or per-VPC/RG
  where the cloud requires it). Delegates to one of the cleaners in
  `lambda/cleaners/`.
- **`lambda/cleaners/`** — one module per cloud provider, each a
  `BaseCleaner` subclass. Adding a new provider means adding one file here
  and one entry in `cleaners/__init__.py`'s `CLEANER_REGISTRY` — no other
  code changes.
- **Credentials** — AWS creds come from the Lambda's own IAM role (nothing
  to configure). Azure/GCP creds are service-principal/service-account JSON
  stored in Secrets Manager, referenced by ARN via SAM parameters.

See `docs/ARCHITECTURE.md` for more detail, and `SECURITY.md` for how
credentials and auth are handled.

## Quick start

Prerequisites: AWS CLI, [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html),
Python 3.12, and an AWS account you're allowed to deploy Lambda/API Gateway/
CloudFront/DynamoDB/IAM resources into.

```bash
git clone <this-repo>
cd aviatrix-cleanup
./setup/deploy.sh
```

The script walks you through:
1. Verifying your AWS CLI credentials work.
2. Generating an auth signing key and setting a login passphrase.
3. Optionally enabling Azure and/or GCP cleanup (creates the Secrets Manager
   secrets for you from pasted JSON).
4. `sam build && sam deploy --guided`.

When it finishes, open the `WebUrl` output in a browser and log in with the
passphrase you set.

To deploy manually instead, see `docs/DEPLOYMENT.md`.

## Repository layout

```
template.yaml          SAM template — all AWS resources
lambda/                 Lambda function source
  cleanup_routes.py      API handler
  cleanup_worker.py      async worker entrypoint
  cleaners/              per-cloud cleanup logic (aws.py / azure.py / gcp.py)
  instances_routes.py    instance list/start/stop (AWS EC2, Azure VM, GCP)
  credentials.py         Secrets Manager credential fetchers
  job_store.py           DynamoDB job-status helpers
  regions.py             region lists shared by routes + instances
  auth_login.py          login/logout, sets the auth cookie
  auth_authorizer.py     API Gateway Lambda authorizer
  schedule_runner.py      daily auto-stop, fired by EventBridge Scheduler
layer/                  Azure/GCP SDK layer requirements (Azure SDK is
                         pre-built and pinned — see docs/AZURE_LAYER.md)
web/                    static web app (Alpine.js)
setup/deploy.sh         guided first-time deploy script
```

## Updating an existing deployment

Code-only changes to a single Lambda can be pushed directly without a full
stack update:

```bash
sam build
BUILD=.aws-sam/build/CleanupWorkerFunction   # or CleanupRoutesFunction, etc.
zip -r /tmp/deploy.zip "$BUILD" -q
aws lambda update-function-code --function-name aviatrix-cleanup-worker \
  --zip-file fileb:///tmp/deploy.zip
```

Changes to `template.yaml` (new routes, IAM, resources) need a full
`sam deploy`.

Static web changes: `aws s3 sync ./web/ s3://<WebBucketName output>/` then
invalidate CloudFront (`aws cloudfront create-invalidation --distribution-id
<CloudFrontDistributionId output> --paths '/*'`).
