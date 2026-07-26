# Manual deployment

`./setup/deploy.sh` automates all of this — use this doc if you want to
understand or customize the steps it runs.

## 1. Prerequisites

- AWS CLI configured with credentials for the target account
  (`aws sts get-caller-identity` should succeed)
- [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- Python 3.12

## 2. (Optional) Isolated deploy — deploy your own copy

If you're deploying your own independent copy of this stack (e.g. for
testing, or so multiple teammates can each have a fully separate
environment in the same AWS account), decide on a short prefix now
(letters/numbers/hyphens, e.g. your username) — you'll pass it as the
`ResourcePrefix` parameter in step 4 (already including its trailing
hyphen, e.g. `alice-`) and use it below wherever a secret/resource name
appears. Leave it blank to deploy/update the canonical shared
`aviatrix-cleanup` stack.

## 3. (Optional) Create Azure/GCP secrets

Skip either of these if you don't need that cloud's cleanup/instances
support — leave the corresponding parameter blank in step 4. Prefix the
secret name with your `ResourcePrefix` from step 2 if you're doing an
isolated deploy (e.g. `alice-aviatrix-cleanup/azure-sp`).

```bash
# Azure — paste your service principal's tenantId/clientId/clientSecret,
# and the subscription ID you want cleanup scoped to.
aws secretsmanager create-secret \
  --name aviatrix-cleanup/azure-sp \
  --secret-string '{"tenantId":"...","clientId":"...","clientSecret":"...","subscriptionId":"..."}'

# GCP — paste the full service account key JSON you downloaded from
# IAM & Admin > Service Accounts > Keys.
aws secretsmanager create-secret \
  --name aviatrix-cleanup/gcp-sa \
  --secret-string file://path/to/your-gcp-sa-key.json
```

Note the ARNs returned — you'll need them in step 4.

If enabling Azure, also build the Azure SDK layer first — see
`docs/AZURE_LAYER.md`.

## 4. Build

```bash
sam build
```

## 5. Deploy

If doing an isolated deploy (step 2), give the stack its own name and
config file so it doesn't touch the shared production stack's:

```bash
sam deploy --guided --stack-name <prefix>-aviatrix-cleanup --config-file samconfig.<prefix>.toml
```

Otherwise, for the canonical shared stack:

```bash
sam deploy --guided
```

On first deploy, SAM will prompt for each parameter:

| Parameter | Value |
|---|---|
| `StageName` | `prod` (or leave default) |
| `ResourcePrefix` | your prefix from step 2 including trailing hyphen (e.g. `alice-`), or blank |
| `AzureSpSecretArn` | ARN from step 3, or blank to disable Azure |
| `GcpSaSecretArn` | ARN from step 3, or blank to disable GCP |
| `AzureSdkLayerArn` | ARN from `docs/AZURE_LAYER.md`, or blank |
| `LoginPassword` | a passphrase (min 12 chars) for the web login |
| `AuthSigningKey` | output of `openssl rand -hex 32` |

Answer "Y" to save these to the config file so future deploys don't
re-prompt (CloudFormation also persists `NoEcho` parameters like
`LoginPassword`/`AuthSigningKey` across deploys regardless). If you used
a per-prefix config file, it's gitignored — don't commit it.

## 6. Build and upload the web app

```bash
(cd web && npm install && npm run build)
aws s3 sync ./web/dist/ s3://<WebBucketName output>/ --delete
aws cloudfront create-invalidation --distribution-id <CloudFrontDistributionId output> --paths '/*'
```

## 7. Open it

Visit the `WebUrl` output and log in with the passphrase from step 5.

## Tearing down an isolated deploy

```bash
./setup/teardown.sh <prefix>
```

`sam delete` alone isn't enough here: CloudFormation refuses to delete a
non-empty S3 bucket (the web bucket always has files in it after deploy),
and two other things aren't stack resources at all — the EventBridge
Scheduler entry `<prefix>-aviatrix-cleanup-stop-daily` (created
imperatively by the Lambda if you ever set a schedule through the app) and
the Azure SDK layer staging bucket `<prefix>aviatrix-cleanup-layer-staging-<account-id>`
(created by `setup/deploy.sh` to publish the layer). `setup/teardown.sh`
deletes the schedule if present, empties the web bucket, removes the layer
staging bucket if present, then runs `sam delete` — so nothing is left
behind and nothing needs to be done in a specific order by hand.

If you'd rather do it manually, or the script isn't available, the
individual steps are:

```bash
aws scheduler delete-schedule --name <prefix>-aviatrix-cleanup-stop-daily --group-name default
aws s3 rm s3://<prefix>-aviatrix-cleanup-web-<account-id>/ --recursive
aws s3 rb s3://<prefix>aviatrix-cleanup-layer-staging-<account-id> --force
sam delete --stack-name aviatrix-cleanup-<prefix> --config-file samconfig.<prefix>.toml
```
