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

`sam delete --stack-name <prefix>-aviatrix-cleanup --config-file samconfig.<prefix>.toml`
removes the CloudFormation stack, but **not** the EventBridge Scheduler
entry `<prefix>-aviatrix-cleanup-stop-daily` if you ever set a schedule
through the app — that's created imperatively by the Lambda, not as a
stack resource. Delete it manually if present:

```bash
aws scheduler delete-schedule --name <prefix>-aviatrix-cleanup-stop-daily --group-name default
```

Also empty the S3 web bucket before deleting the stack — CloudFormation
won't delete a non-empty bucket:

```bash
aws s3 rm s3://<prefix>-aviatrix-cleanup-web-<account-id>/ --recursive
```

If Azure support was enabled, `setup/deploy.sh` also creates a small S3
bucket (`<prefix>aviatrix-cleanup-layer-staging-<account-id>`) to stage the
Azure SDK layer zip during publish — also not a stack resource, so it
survives `sam delete` too. Delete it once you no longer need to
redeploy/update the Azure layer:

```bash
aws s3 rb s3://<prefix>aviatrix-cleanup-layer-staging-<account-id> --force
```
