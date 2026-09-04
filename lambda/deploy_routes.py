"""
deploy_routes.py — "Deploy" tab: free-text -> Claude Code -> Terraform apply
Routes:
  POST /api/deploy         — start a deploy job (async, via SSM Send-Command)
  GET  /api/deploy/status  — poll job status by jobId

Flow:
  User text -> this Lambda -> ssm:SendCommand -> EC2 runner (Terraform +
  Claude Code installed) -> Claude Code interprets the request and runs
  `terraform apply` against the official Aviatrix control-plane module ->
  Aviatrix Controller/Copilot live in AWS.

There is deliberately no separate "intent parsing" LLM call here and no
approval gate before apply — Claude Code itself (running headless on the
runner) is the interpreter, with full latitude to write/adjust Terraform.
Every session's full transcript + Terraform plan/apply output is mirrored to
S3 by SSM itself (OutputS3BucketName/Prefix below) so there's always an audit
trail, even without a human-in-the-loop confirmation step.
"""

import base64
import json
import os
import re
import uuid
import boto3
from datetime import datetime, timezone

REGION            = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME        = os.environ.get("CLEANUP_TABLE", "aviatrix-cleanup-jobs")
RUNNER_INSTANCE_ID = os.environ.get("DEPLOY_RUNNER_INSTANCE_ID", "")
ARTIFACTS_BUCKET  = os.environ.get("DEPLOY_ARTIFACTS_BUCKET", "")
CLAUDE_CREDS_SECRET_ARN = os.environ.get("CLAUDE_CODE_CREDS_SECRET_ARN", "")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
ssm      = boto3.client("ssm", region_name=REGION)
s3       = boto3.client("s3", region_name=REGION)

DEPLOY_RESULT_RE = re.compile(r"^DEPLOY_RESULT:\s*(\{.*\})\s*$")


def _resp(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def _table():
    return dynamodb.Table(TABLE_NAME)


# ── Task script — what actually runs on the runner for one job ─────────────

_TASK_SCRIPT_TEMPLATE = r"""#!/bin/bash
# Runs as root (SSM Run Command default). Everything below is written to a
# file and executed as an unprivileged user instead -- Claude Code refuses
# --dangerously-skip-permissions when its own euid is 0.
set -uo pipefail
JOB_ID="__JOB_ID__"
WORKDIR="/opt/aviatrix-deploy/$JOB_ID"
mkdir -p "$WORKDIR"

cat > "$WORKDIR/run.sh" <<'RUNSCRIPT_EOF'
#!/bin/bash
set -uo pipefail
cd "$(dirname "$0")"

echo "__PROMPT_B64__" | base64 -d > prompt.txt

cat > SYSTEM.md <<'SYSTEMPROMPT_EOF'
You are deploying Aviatrix control-plane infrastructure into this AWS account
via Terraform, on behalf of a lab user who submitted a free-text request.

Ground rules:
- AWS credentials come from this EC2 instance's IAM role. Never ask for or
  invent credentials. The deployment region is __AWS_REGION__.
- Your own model calls may be routed through Amazon Bedrock in a different
  AWS region (via AWS_REGION/AWS_BEARER_TOKEN_BEDROCK in your environment).
  That region is NOT the deployment region and must never be used for
  infrastructure. Always hardcode `region = "__AWS_REGION__"` in every AWS
  Terraform provider block you write, and always pass `--region __AWS_REGION__`
  explicitly on any raw `aws` CLI command — do not rely on ambient
  AWS_REGION/AWS_DEFAULT_REGION for infrastructure operations.
- Unless the request clearly calls for something else, use the official
  Terraform module `terraform-aviatrix-modules/aws-controlplane/aviatrix`
  (Terraform Registry) as your starting point for controller/copilot
  buildouts. Read its inputs/outputs (terraform registry docs or
  `terraform providers schema` after init) before writing variables.
- Work only inside the current directory ($WORKDIR). Initialize a fresh
  Terraform config here, run `terraform init`, `terraform plan`, then
  `terraform apply -auto-approve`. There is no human review step for this
  request, so proceed straight to apply once your configuration looks
  correct to you.
- If AWS Marketplace subscription for the Controller/Copilot AMI has not
  been accepted in this account/region yet, `terraform apply` will fail with
  a clear marketplace-subscription error — report that verbatim rather than
  retrying blindly.
- After a successful apply, run `terraform output -json > tf-outputs.json`
  in $WORKDIR so the result can be parsed by the caller.
- As your final action (success or failure), print exactly one line of the
  form below (valid single-line JSON, no other text on that line):
  DEPLOY_RESULT: {"status": "success", "summary": "...", "controller_public_ip": "...", "copilot_public_ip": "...", "controller_url": "...", "details": "..."}
  Use "status": "error" and fill "details" with what went wrong if you did
  not reach a successful apply.
SYSTEMPROMPT_EOF

aws secretsmanager get-secret-value --secret-id '__SECRET_ARN__' --query SecretString \
  --output text --region __AWS_REGION__ > claude_creds.json 2>secret_err.log
if [ ! -s claude_creds.json ]; then
  python3 - <<'SECRETERR_EOF'
import json
try:
    detail = open("secret_err.log").read().strip()
except Exception:
    detail = ""
print("DEPLOY_RESULT: " + json.dumps({
    "status": "error",
    "summary": "could not read Claude Code credentials from Secrets Manager",
    "details": detail,
}))
SECRETERR_EOF
  exit 1
fi

# The secret is JSON holding either a direct API key
# ({"ANTHROPIC_API_KEY": "sk-ant-..."}) or Bedrock-routed creds
# ({"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_BEARER_TOKEN_BEDROCK": "...",
# "ANTHROPIC_DEFAULT_SONNET_MODEL": "...", "BEDROCK_REGION": "..."}).
# BEDROCK_REGION (or a bare AWS_REGION key) is normalized to AWS_REGION here
# purely for Claude Code's own model calls -- see the AWS_REGION warning in
# SYSTEM.md above; this script re-anchors AWS_DEFAULT_REGION right after.
python3 - <<'CREDSPY_EOF'
import json
d = json.load(open("claude_creds.json"))
region = d.pop("BEDROCK_REGION", None) or d.pop("AWS_REGION", None)
if region:
    d["AWS_REGION"] = region
with open("claude_creds.env", "w") as f:
    for k, v in d.items():
        f.write(f"{k}={v}\n")
CREDSPY_EOF
rm -f claude_creds.json secret_err.log

set -a
. ./claude_creds.env
set +a
rm -f claude_creds.env
export AWS_DEFAULT_REGION="__AWS_REGION__"

claude --print --bare --dangerously-skip-permissions --output-format json \
  --append-system-prompt-file SYSTEM.md \
  "$(cat prompt.txt)" > claude-result.json 2> claude-stderr.log
CLAUDE_EXIT=$?

echo "----- claude-result.json -----"
cat claude-result.json 2>/dev/null || true
echo "----- claude-stderr.log (tail) -----"
tail -c 4000 claude-stderr.log 2>/dev/null || true
echo "----- claude exit code: $CLAUDE_EXIT -----"

python3 - <<'PYEOF'
import json, re, sys
try:
    d = json.load(open("claude-result.json"))
    text = d.get("result", "") if isinstance(d, dict) else ""
except Exception:
    text = ""
marker = None
for line in text.splitlines():
    if line.strip().startswith("DEPLOY_RESULT:"):
        marker = line.strip()
        break
if marker:
    print(marker)
else:
    print('DEPLOY_RESULT: {"status": "unknown", "summary": "no DEPLOY_RESULT marker found in Claude output; see full session log", "details": ""}')
PYEOF
RUNSCRIPT_EOF

chown -R deployrunner:deployrunner "$WORKDIR"
chmod +x "$WORKDIR/run.sh"
sudo -u deployrunner -H bash "$WORKDIR/run.sh"
"""


def _build_task_script(job_id: str, prompt: str) -> str:
    prompt_b64 = base64.b64encode(prompt.encode()).decode()
    script = _TASK_SCRIPT_TEMPLATE
    script = script.replace("__JOB_ID__", job_id)
    script = script.replace("__PROMPT_B64__", prompt_b64)
    script = script.replace("__SECRET_ARN__", CLAUDE_CREDS_SECRET_ARN)
    script = script.replace("__AWS_REGION__", REGION)
    return script


# ── POST /api/deploy ─────────────────────────────────────────────────────────

def start_deploy(event: dict) -> dict:
    """
    Body (JSON):
      prompt   free-text description of what to deploy (required)
    """
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "Invalid JSON body"})

    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return _resp(400, {"error": "prompt is required"})
    if len(prompt) > 4000:
        return _resp(400, {"error": "prompt too long (max 4000 chars)"})
    if not RUNNER_INSTANCE_ID:
        return _resp(500, {"error": "Deploy feature not configured (no runner instance / Anthropic secret)"})

    job_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    script = _build_task_script(job_id, prompt)

    try:
        send_resp = ssm.send_command(
            InstanceIds=[RUNNER_INSTANCE_ID],
            DocumentName="AWS-RunShellScript",
            Comment=f"aviatrix-cleanup deploy job {job_id}",
            Parameters={"commands": [script]},
            TimeoutSeconds=3600,
            OutputS3BucketName=ARTIFACTS_BUCKET,
            OutputS3KeyPrefix=f"ssm-output/{job_id}",
        )
    except Exception as exc:
        return _resp(502, {"error": f"failed to dispatch to runner: {exc}"})

    command_id = send_resp["Command"]["CommandId"]

    _table().put_item(Item={
        "jobId":      job_id,
        "kind":       "deploy",
        "prompt":     prompt,
        "commandId":  command_id,
        "instanceId": RUNNER_INSTANCE_ID,
        "status":     "PENDING",
        "steps":      [],
        "createdAt":  now_iso,
        "updatedAt":  now_iso,
    })

    return _resp(202, {
        "jobId":  job_id,
        "status": "PENDING",
        "message": "Deploy job dispatched to runner",
    })


# ── GET /api/deploy/status ───────────────────────────────────────────────────

_SSM_TO_JOB_STATUS = {
    "Pending":      "PENDING",
    "InProgress":   "RUNNING",
    "Delayed":      "RUNNING",
    "Success":      "COMPLETE",
    "Cancelled":    "ERROR",
    "TimedOut":     "ERROR",
    "Failed":       "ERROR",
    "Cancelling":   "RUNNING",
}


def _fetch_full_s3_output(job_id: str, instance_id: str, stream: str) -> str:
    """SSM's GetCommandInvocation truncates Standard*Content; the full text
    lives in S3 (mirrored automatically because send_command was called with
    OutputS3BucketName/Prefix). stream is 'stdout' or 'stderr'."""
    key = f"ssm-output/{job_id}/{instance_id}/awsrunShellScript/0.awsrunShellScript/{stream}"
    try:
        obj = s3.get_object(Bucket=ARTIFACTS_BUCKET, Key=key)
        return obj["Body"].read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def get_deploy_status(event: dict) -> dict:
    params = event.get("queryStringParameters") or {}
    job_id = params.get("jobId", "").strip()
    if not job_id:
        return _resp(400, {"error": "jobId query parameter is required"})

    item = _table().get_item(Key={"jobId": job_id}).get("Item")
    if not item:
        return _resp(404, {"error": f"Job '{job_id}' not found"})

    command_id  = item.get("commandId")
    instance_id = item.get("instanceId")

    # Only re-poll SSM while the job isn't already terminal — once COMPLETE/
    # ERROR is recorded, avoid re-fetching (S3 log objects are the durable
    # record; SSM's own invocation record eventually expires).
    if item.get("status") in ("PENDING", "RUNNING") and command_id and instance_id:
        try:
            inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            ssm_status = inv.get("Status", "Pending")
            job_status = _SSM_TO_JOB_STATUS.get(ssm_status, "RUNNING")
            stdout = inv.get("StandardOutputContent", "")
            stderr = inv.get("StandardErrorContent", "")

            result = None
            for line in stdout.splitlines():
                m = DEPLOY_RESULT_RE.match(line.strip())
                if m:
                    try:
                        result = json.loads(m.group(1))
                    except json.JSONDecodeError:
                        pass

            # Full (untruncated) output only needed once the run is finished —
            # cheaper than fetching from S3 on every poll while still running.
            full_stdout = stdout
            if job_status in ("COMPLETE", "ERROR"):
                fetched = _fetch_full_s3_output(job_id, instance_id, "stdout")
                if fetched:
                    full_stdout = fetched
                    if not result:
                        for line in fetched.splitlines():
                            m = DEPLOY_RESULT_RE.match(line.strip())
                            if m:
                                try:
                                    result = json.loads(m.group(1))
                                except json.JSONDecodeError:
                                    pass

            update_expr = (
                "SET #s = :s, updatedAt = :u, ssmStatus = :ss, "
                "logTail = :lt, stderrTail = :et"
            )
            expr_values = {
                ":s":  job_status,
                ":u":  datetime.now(timezone.utc).isoformat(),
                ":ss": ssm_status,
                ":lt": full_stdout[-6000:],
                ":et": stderr[-2000:],
            }
            if result:
                update_expr += ", deployResult = :dr"
                expr_values[":dr"] = result

            _table().update_item(
                Key={"jobId": job_id},
                UpdateExpression=update_expr,
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=expr_values,
            )
            item["status"]       = job_status
            item["ssmStatus"]    = ssm_status
            item["logTail"]      = full_stdout[-6000:]
            item["stderrTail"]   = stderr[-2000:]
            if result:
                item["deployResult"] = result
        except ssm.exceptions.InvocationDoesNotExist:
            pass
        except Exception as exc:
            item["pollError"] = str(exc)

    return _resp(200, item)


# ── Lambda entrypoint (called from cleanup_routes.py) ───────────────────────

def route(action: str, event: dict) -> dict:
    if action == "start":
        return start_deploy(event)
    if action == "status":
        return get_deploy_status(event)
    return _resp(404, {"error": f"unknown deploy action '{action}'"})
