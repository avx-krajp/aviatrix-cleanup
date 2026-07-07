# Security

## Credential handling

| Cloud | How credentials work |
|-------|----------------------|
| AWS   | The Lambda functions use their own IAM execution role. No credentials to configure or store — the role's policy (in `template.yaml`) grants exactly the actions each function needs. |
| Azure | A service principal's `{tenantId, clientId, clientSecret, subscriptionId}` JSON is stored in AWS Secrets Manager. The secret's ARN is passed to the stack as the `AzureSpSecretArn` parameter. Lambdas fetch it at runtime via `secretsmanager:GetSecretValue` (scoped to that one secret ARN only). |
| GCP   | A service account key JSON is stored in Secrets Manager, referenced via the `GcpSaSecretArn` parameter, fetched the same way. |

Nothing sensitive is ever stored in `template.yaml`, environment variables
beyond the secret ARN itself, or the web frontend. The login passphrase
(`LoginPassword`) and cookie-signing key (`AuthSigningKey`) are `NoEcho`
CloudFormation parameters — set once on first deploy, then persisted by
CloudFormation across future deploys without needing to be passed again.

**Never commit real secret values, ARNs with account IDs you don't want
public, or `.claude/settings.local.json`** (Claude Code's local permission
file can end up with credentials pasted into it during interactive
sessions) — see `.gitignore`.

## Authentication

- The web app and all `/api/*` routes are gated by a signed HMAC cookie
  (`cm_auth`), set on login (`auth_login.py`) and verified by both a
  CloudFront Function (for static files) and a Lambda authorizer
  (`auth_authorizer.py`, for API routes).
- There is a single shared passphrase (`LoginPassword`), not per-user
  accounts — appropriate for a small-team lab tool, not for a
  multi-tenant or public deployment.
- `/api/instances*` (list/start/stop cloud VMs) is gated by the same cookie
  authorizer as `/api/cleanup*`. Do not add these routes with
  `Authorizer: NONE` — an unauthenticated instances API lets anyone with
  the URL list and stop/start every VM in the account.

## IAM scope

The worker Lambda's IAM policy is broad by necessity (it deletes ~50
different AWS resource types across networking, compute, storage, and
managed services). Review `template.yaml`'s `CleanupWorkerFunction` policy
before deploying into a shared or production-adjacent account — this tool
is designed for lab/sandbox accounts where broad delete permissions are
acceptable.

## Reporting a concern

If you find a security issue in this tool, open an issue or contact the
maintainer directly rather than filing a public issue with exploit details.
