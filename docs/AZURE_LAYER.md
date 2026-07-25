# Building the Azure SDK Lambda layer

Azure cleanup and instance support need the `azure-mgmt-*`/`azure-identity`
packages, which are built as a separate Lambda layer rather than bundled
into the function zip. Installed as-is, these packages total ~500MB —
well over Lambda's 250MB unzipped layer limit — because each `azure-mgmt-*`
package vendors every historical API version it has ever supported as a
separate submodule (`v2019_03_01/`, `v2024_07_01/`, etc.), even though this
app's code (`lambda/cleaners/azure.py`) never passes `api_version=`
explicitly and so only ever exercises each client's own current default.
`layer/azure-sdk/trim_unused_api_versions.py` removes every version
submodule that isn't referenced by a client's own default/profile — see
the script's docstring for how it determines what's safe to remove. This
brings the installed size down to ~140MB.

If you don't need Azure support, leave `AzureSdkLayerArn` blank — the
Azure cleaner and Azure instance routes will raise a clear error if
invoked without `AzureSpSecretArn` configured, and everything else works
normally.

## Automated (via `setup/deploy.sh`)

If you enable Azure support in the guided deploy script, it builds,
trims, and publishes this layer for you automatically (or reuses an
existing one already published under this deploy's prefix) — no manual
steps needed. Use the manual steps below only if you're deploying by hand
(see `docs/DEPLOYMENT.md`) or the automated build fails (e.g. no network
access to PyPI).

## Build it manually

```bash
cd layer/azure-sdk
pip install -r requirements.txt -t python/ --platform manylinux2014_x86_64 \
  --only-binary=:all: --python-version 3.12
python3 trim_unused_api_versions.py python/
zip -r azure-sdk-layer.zip python/
```

`aws lambda publish-layer-version --zip-file` only supports payloads up to
~50MB (it uploads inline as base64) — even the trimmed layer exceeds that,
so stage it via S3 instead:

```bash
aws s3 mb s3://<your-staging-bucket> --region <region>   # skip if it already exists
aws s3 cp azure-sdk-layer.zip s3://<your-staging-bucket>/azure-sdk-layer.zip
aws lambda publish-layer-version \
  --layer-name aviatrix-cleanup-azure-sdk \
  --content S3Bucket=<your-staging-bucket>,S3Key=azure-sdk-layer.zip \
  --compatible-runtimes python3.12
```

Take the `LayerVersionArn` from the output and pass it as
`AzureSdkLayerArn` in your `sam deploy --parameter-overrides`.

To update the layer later (e.g. new Azure SDK versions), publish a new
version and update the `AzureSdkLayerArn` parameter — old versions stay
around until you delete them, so a bad update is easy to roll back.
