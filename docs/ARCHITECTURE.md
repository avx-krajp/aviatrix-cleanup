# Architecture

## Components

**Web app** (`web/`) — Alpine.js SPA served from a private S3 bucket,
fronted by CloudFront. A CloudFront Function checks the `cm_auth` cookie on
every static-file request and redirects to `/login.html` if missing/invalid.

**API** — a single HTTP API Gateway, with `/api/*` proxied by CloudFront to
API Gateway's own domain (so the browser only ever talks to one origin).
Routes:

| Route | Lambda | Auth |
|---|---|---|
| `POST /api/login`, `POST /api/logout` | `auth_login.py` | none (issues the cookie) |
| `POST /api/cleanup` | `cleanup_routes.py` | cookie |
| `GET /api/cleanup/status` | `cleanup_routes.py` | cookie |
| `GET/PUT/POST /api/schedule*` | `cleanup_routes.py` | cookie |
| `GET /api/instances`, `POST /api/instances/{start,stop,start-all,stop-all}` | `cleanup_routes.py` → `instances_routes.py` | cookie |

All cookie-gated routes share one Lambda authorizer (`auth_authorizer.py`),
wired via `FunctionArn` in `template.yaml`. **Important:** when an
authorizer is wired this way (rather than declared inline), API Gateway
needs an explicit `AWS::Lambda::Permission` granting it invoke rights — SAM
does not create this automatically. Without it, every `/api/*` call returns
500. See `AuthAuthorizerInvokePermission` in `template.yaml`.

## Cleanup job flow

1. `POST /api/cleanup` with `{cloud, region, vpc_id?, dry_run}`.
   - If `region == "all-regions"`, `cleanup_routes.py` fans out one child
     job per region into DynamoDB (`aviatrix-cleanup-jobs`) and
     async-invokes `cleanup_worker.py` once per region.
   - GCP's all-regions fan-out marks exactly one region as
     `is_primary_region` — only that worker touches GCP's *global*
     resources (firewalls, routes, VPC networks) to avoid ~40 parallel
     workers racing on the same global API endpoints.
2. `cleanup_worker.py`'s `handler()` looks up the right cleaner class from
   `cleaners.CLEANER_REGISTRY` by the `cloud` field and calls `.run()`.
3. Each cleaner (`AWSCleaner`, `AzureCleaner`, `GCPCleaner`, all in
   `lambda/cleaners/`) runs a fixed sequence of numbered `stepN_*` methods,
   each emitting a step record (`running` → `done`/`error`/`skipped`) to
   DynamoDB so the UI can poll live progress via `GET /api/cleanup/status`.
4. For an all-regions parent job, `get_status()` aggregates all child job
   statuses and marks the parent `COMPLETE` once every child has finished.

### Adding a new cloud provider

Write `lambda/cleaners/<newcloud>.py` with a class subclassing `BaseCleaner`
(from `cleaners/base.py`) that implements `__init__` (set up API clients)
and `run()` (call your `stepN_*` methods in order). Add one entry to
`CLEANER_REGISTRY` in `cleaners/__init__.py`. No changes needed to
`cleanup_worker.py`, `cleanup_routes.py`, or `template.yaml` routing logic
— only the new IAM permissions the cleaner needs.

## Known per-cloud quirks

- **AWS** — Aviatrix's GuardDuty Runtime Monitoring feature auto-attaches
  ENIs to any VPC with EC2 instances, which block subnet/VPC teardown.
  `step12_eni` polls (up to 5 min) for GuardDuty to retract them naturally
  after instance termination, rather than calling GuardDuty APIs directly.
- **Azure** — the controller's own resource group (`avx-controller_group`)
  carries no tags at all, so RG discovery matches on tag key OR an
  `avx-` name prefix.
- **GCP** — no SDK is used (kept out of the Lambda layer due to size);
  all calls are raw REST via `google-auth`'s `AuthorizedSession`. Peer
  networks (`gcp-spoke`/`gcp-transit`) can end up with zero remaining
  tagged resources except an `avx-` route, which is why route ownership is
  checked as a network-discovery signal, not just network name.

## Instances feature

`instances_routes.py` lists/starts/stops EC2 instances, Azure VMs, and GCP
Compute instances — separate from the cleanup flow, used for the web UI's
instance dashboard and the daily auto-stop schedule
(`schedule_runner.py`, fired by EventBridge Scheduler). It deliberately
avoids the Azure/GCP SDKs (raw OAuth via `urllib` for Azure, `google-auth`
REST for GCP) so it can run in the lighter `CleanupRoutesFunction`, which
has no Azure SDK layer attached.

`schedule_runner.py` calls into `instances_routes.py`'s list/stop helpers
as plain Python function calls rather than HTTP, since it runs on its own
schedule with no browser cookie to authenticate an API Gateway round-trip.
