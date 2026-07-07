# Manual deployment

`./setup/deploy.sh` automates all of this — use this doc if you want to
understand or customize the steps it runs.

## 1. Prerequisites

- AWS CLI configured with credentials for the target account
  (`aws sts get-caller-identity` should succeed)
- [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- Python 3.12

## 2. (Optional) Create Azure/GCP secrets

Skip either of these if you don't need that cloud's cleanup/instances
support — leave the corresponding parameter blank in step 4.

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

## 3. Build

```bash
sam build
```

## 4. Deploy

```bash
sam deploy --guided
```

On first deploy, SAM will prompt for each parameter:

| Parameter | Value |
|---|---|
| `StageName` | `prod` (or leave default) |
| `AzureSpSecretArn` | ARN from step 2, or blank to disable Azure |
| `GcpSaSecretArn` | ARN from step 2, or blank to disable GCP |
| `AzureSdkLayerArn` | ARN from `docs/AZURE_LAYER.md`, or blank |
| `LoginPassword` | a passphrase (min 12 chars) for the web login |
| `AuthSigningKey` | output of `openssl rand -hex 32` |

Answer "Y" to save these to `samconfig.toml` so future deploys don't
re-prompt (CloudFormation also persists `NoEcho` parameters like
`LoginPassword`/`AuthSigningKey` across deploys regardless).

## 5. Upload the web app

```bash
aws s3 sync ./web/ s3://<WebBucketName output>/
aws cloudfront create-invalidation --distribution-id <CloudFrontDistributionId output> --paths '/*'
```

## 6. Open it

Visit the `WebUrl` output and log in with the passphrase from step 4.
