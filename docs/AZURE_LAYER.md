# Building the Azure SDK Lambda layer

Azure cleanup and instance support need the `azure-mgmt-*`/`azure-identity`
packages, which are large enough that they're built as a separate Lambda
layer rather than bundled into the function zip — combined, they sit close
to Lambda's 250 MB unzipped limit, so the layer is built once and pinned by
ARN (`AzureSdkLayerArn`) rather than rebuilt on every `sam deploy`.

If you don't need Azure support, leave `AzureSdkLayerArn` blank — the
Azure cleaner and Azure instance routes will raise a clear error if
invoked without `AzureSpSecretArn` configured, and everything else works
normally.

## Build it once

```bash
cd layer/azure-sdk
pip install -r requirements.txt -t python/ --platform manylinux2014_x86_64 \
  --only-binary=:all: --python-version 3.12
zip -r azure-sdk-layer.zip python/
aws lambda publish-layer-version \
  --layer-name aviatrix-cleanup-azure-sdk \
  --zip-file fileb://azure-sdk-layer.zip \
  --compatible-runtimes python3.12
```

Take the `LayerVersionArn` from the output and pass it as
`AzureSdkLayerArn` in your `sam deploy --parameter-overrides`.

To update the layer later (e.g. new Azure SDK versions), publish a new
version and update the `AzureSdkLayerArn` parameter — old versions stay
around until you delete them, so a bad update is easy to roll back.
