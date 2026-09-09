# Capped AWS YOLO gateway

This directory packages an evaluated single-class pothole ONNX model as an
authenticated server-to-server AWS endpoint:

```text
Pothole Reporter Worker
  -> API Gateway HTTP API (AWS_IAM + rate/burst throttle)
  -> Lambda container (reserved-concurrency ceiling)
  -> DynamoDB conditional monthly admission
  -> ONNX CPU inference
```

It is the fallback detector for `SHARED_DETECTOR_PROVIDER=openai_then_http_yolo`.
It is not a second public app API, a repair verifier, or a tender matcher. API Gateway
requires an AWS SigV4 identity with `execute-api:Invoke` before Lambda can run. Lambda
then hash-verifies a separate project gateway token as defense in depth. The URL remains
internet-routable, so keep its credentials out of apps and browsers.

## What the caps mean

After edge IAM authorization and Lambda token/envelope checks, a consistent read rejects an already exhausted month
before PIL image decode. Both configured monthly caps are then enforced authoritatively
in one conditional DynamoDB update before the model is initialized or run:

1. `monthly_request_cap`: maximum admitted inference attempts in a UTC month.
2. `monthly_estimated_budget_usd`: maximum sum of the configured conservative
   `estimated_cost_per_request_usd` in that month.

The preflight is only a cost guard; the conditional update remains the race-safe
admission authority. The first cap reached returns HTTP 429. A zero cap is a kill switch. An admitted
attempt remains counted if model loading or inference fails; this deliberately fails
closed under repeated errors. A DynamoDB outage also prevents inference.

The second cap is a hard cap on the **configured cost estimate**, not on the final AWS
invoice. Set the estimate from a measured worst case that includes the configured
memory and timeout, API Gateway, DynamoDB, and logs. Invalid/unauthenticated traffic,
ECR storage, rejected/over-cap Lambda execution, and other account resources can still
create charges without being admitted. AWS itself warns that billing data and Budget notifications can be delayed,
so the optional Terraform AWS Budget is advisory and account-wide; it is not used in
the request path. See [AWS Budgets timing](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html).

API Gateway has a 10 MB fixed payload quota, but Lambda's synchronous invocation
payload ceiling is tighter at 6 MB and includes the proxy event wrapper. The gateway
therefore accepts exactly one image of at most 3.5 MB and limits the inner JSON body to
5.5 MB, leaving
margin for base64 expansion, headers, and the event envelope. Lambda reserved
concurrency is a no-additional-charge maximum and does not pre-warm the model. See the
official [HTTP API quotas](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-quotas.html),
[Lambda quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html),
and [Lambda concurrency behavior](https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html).

The default Lambda timeout is 27 seconds and the separate API Gateway integration
timeout is 29 seconds. Terraform requires both to be whole seconds, limits the API
timeout to the HTTP API maximum of 30 seconds, and refuses a deployment unless the
integration timeout is strictly greater than the Lambda timeout. This headroom lets
Lambda reach its own deadline and return a controlled error before API Gateway closes
the integration. When tuning inference latency, change and benchmark both values while
preserving strict headroom; never set them equal.

## Detection contract

`POST /v1/detect` requires prompt `road-damage-v5`, schema version `4`, a
`capture_mode` of `manual` or `drive`, and exactly one item in `images`. That item
contains only its `data_url`; capture mode supplies the acquisition context. The
single-image boundary keeps the YOLO result independent of capture sequence
heuristics. The response verdict has exactly these fields:

```json
{
  "image_quality": "acceptable",
  "assessment": "damaged",
  "damage_type": "pothole_cavity",
  "size": null,
  "description": "The pothole model detected a cavity on the road surface."
}
```

`image_quality` is `acceptable` or `rejected`; `assessment` is `damaged` or
`undamaged`; `damage_type` is `pothole_cavity` only for a positive detection and is
otherwise `null`. `size` remains `null` unless a future evaluated model has a defensible
scale signal. The runtime applies the model's one evaluated detection-confidence
threshold. It does not use image position, road-edge heuristics, or road-surface material
to suppress a model detection: a pothole candidate at the edge of a paved, gravel, mud,
or dirt road remains eligible.

## Model release prerequisite

There is intentionally no placeholder model. First produce the evaluated release in:

```text
ml/yolo/artifacts/pothole-yolo-v1/model.onnx
ml/yolo/artifacts/pothole-yolo-v1/model-manifest.json
ml/yolo/artifacts/pothole-yolo-v1/runtime-parity.json
```

The current app-owned export has image-level human labels but no bounding boxes. The
referenced public RAD `RoadDamages` class also mixes potholes with other objects. The
training pipeline therefore refuses to release a model until owned positives have
YOLO boxes and public mixed-class boxes have been human-audited as actual potholes.
This prevents a generic road-damage model from being presented as a pothole detector.

The current pipeline uses Ultralytics tooling and base weights. Resolve its AGPL-3.0
versus Enterprise licensing requirement before a proprietary/private production
deployment; see the [Ultralytics licence guidance](https://www.ultralytics.com/license).

Copy an evaluated bundle into the Docker staging directory:

```bash
cp ml/yolo/artifacts/pothole-yolo-v1/model.onnx infra/aws-yolo/artifacts/model.onnx
cp ml/yolo/artifacts/pothole-yolo-v1/model-manifest.json infra/aws-yolo/artifacts/model-manifest.json
cp ml/yolo/artifacts/pothole-yolo-v1/runtime-parity.json infra/aws-yolo/artifacts/runtime-parity.json
```

Cold start verifies that the manifest identifies class 0 as exactly `pothole`, uses a
raw `nms=false` ONNX export, and matches `MODEL_VERSION`. A missing or mismatched
manifest, model hash, parity receipt, production detector-code hash, confidence
threshold, decoded-pixel ceiling, Lambda CPU architecture, Python runtime, or evaluated
NMS settings fails before budget admission. Copy `decision.confidence_threshold` from
`model-manifest.json` into Terraform's `detection_confidence_threshold` value. It is an
evaluated release setting, not an operator-tunable production default.

## Bootstrap and deploy

Nothing in this repository deploys or spends money automatically. Use a dedicated AWS
account or workload role and choose the three cap values explicitly.

1. Generate one strong bearer secret and keep the plaintext out of Terraform:

   ```bash
   YOLO_GATEWAY_TOKEN="$(openssl rand -hex 32)"
   printf '%s' "$YOLO_GATEWAY_TOKEN" | shasum -a 256
   ```

   Put only the lowercase digest in `api_key_sha256`. Store the plaintext for the
   later `wrangler secret put YOLO_API_KEY` step.

2. Bootstrap ECR and the DynamoDB counter without creating a callable endpoint:

   ```bash
   cd infra/aws-yolo/terraform
   cp terraform.tfvars.example terraform.tfvars
   # Replace every placeholder and review all three caps.
   terraform init
   terraform plan
   terraform apply
   terraform output -raw ecr_repository_url
   ```

   Keep `container_image_uri = ""` for this first apply.

3. Build for the configured Lambda architecture, scan, and push. For the default
   `x86_64` architecture:

   ```bash
   AWS_REGION="ap-south-1"
   ECR_REPOSITORY="$(terraform output -raw ecr_repository_url)"
   aws ecr get-login-password --region "$AWS_REGION" \
     | docker login --username AWS --password-stdin "${ECR_REPOSITORY%%/*}"
   cd ..
   docker build --platform linux/amd64 -t "$ECR_REPOSITORY:pothole-yolo-v1" .
   docker push "$ECR_REPOSITORY:pothole-yolo-v1"
   aws ecr describe-images --repository-name pothole-reporter-yolo \
     --image-ids imageTag=pothole-yolo-v1 \
     --query 'imageDetails[0].imageDigest' --output text
   ```

   Review the ECR scan before continuing. Prefer the immutable digest URI
   `repository@sha256:...` over a tag. The lifecycle policy prunes only untagged
   images; deployed and release tags are never matched. Remove a rejected candidate
   tag after evaluation to make that image eligible for cleanup. Give every accepted
   digest a permanent release tag before deploying it because Lambda can fetch the
   container again after deployment. Do not delete that tag or digest while a
   function version can still invoke it. ECR lifecycle rules cannot safely expire a
   `candidate-*` tag directly: if that digest also has a release tag, expiry can remove
   the release image as well.

4. Put that digest URI in `container_image_uri`, run `terraform plan`, and apply a
   second time. Confirm the plan still shows
   `lambda_timeout_seconds < api_integration_timeout_seconds`; the `yolo_api_url`
   output is then the only callable route. Terraform also prints
   `yolo_caller_invoke_arn` and `yolo_caller_policy_json`. Attach that policy to an
   externally managed Worker caller principal; it grants only
   `execute-api:Invoke` on `POST /v1/detect` in the `$default` stage. This stack
   deliberately creates no IAM user or long-lived access key.

5. Smoke-test with a SigV4-capable client and a valid version-1 request, including the
   project token as `X-Yolo-API-Key`. An unsigned request must receive `403` at API
   Gateway without invoking Lambda. Never paste credentials, the token, or base64
   image into logs or shell history shared with others. CloudWatch application logs
   contain request ID, outcome, latency, model version, and aggregate counter values;
   they never include the body or image.

## Connect the central Worker

After the AWS smoke test succeeds:

```bash
cd server
npx wrangler secret put YOLO_API_KEY
npx wrangler secret put YOLO_AWS_ACCESS_KEY_ID
npx wrangler secret put YOLO_AWS_SECRET_ACCESS_KEY
# Only for temporary STS credentials:
npx wrangler secret put YOLO_AWS_SESSION_TOKEN
```

Set the following Worker configuration and deploy it through the existing reviewed
server release process:

```toml
SHARED_DETECTOR_PROVIDER = "openai_then_http_yolo"
YOLO_API_URL = "https://<api-id>.execute-api.ap-south-1.amazonaws.com/v1/detect"
YOLO_MODEL = "pothole-yolo-v1"
YOLO_AWS_REGION = "ap-south-1"
```

The plaintext secret entered into Wrangler must be the token whose hash was supplied
to Terraform. The AWS principal must have only the policy emitted by
`yolo_caller_policy_json`; prefer temporary credentials with automated rotation when
available. The Worker validates the exact HTTPS `execute-api` hostname, region, and
`/v1/detect` path before SigV4 signing. It first calls OpenAI and uses this endpoint only for explicit
OpenAI credit/spend/usage-limit exhaustion. Ordinary OpenAI rate limits, timeouts,
authentication failures, malformed output, or 5xx responses do not silently change
the model used for a citizen report.

## Operations

- Emergency stop: set `reserved_concurrency = 0`, either monthly cap to `0`, or set
  the Worker provider back to `openai`/disable shared inference.
- Model rollback: build a previously evaluated immutable release, update
  `container_image_uri` and `model_version` together, plan, then apply.
- Usage check: read the DynamoDB item keyed by UTC `YYYY-MM`; `requests` and
  `estimated_microusd` are aggregate counters.
- Cost calibration: benchmark p50/p95/p99 duration with real phone and dashcam images,
  then round the worst-case all-in estimate upward before increasing either cap.
- Privacy: do not add request-body execution logging, X-Ray payload annotations, or
  image persistence. The service needs no S3 bucket and no outbound network access.

The counter uses an atomic `UpdateItem` plus `ConditionExpression`, the mechanism AWS
documents for conditional writes and atomic counters. See
[DynamoDB item operations](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/WorkingWithItems.html).

## Local verification

Use the exact Lambda dependencies so the raw-tensor, media-safety, and NMS tests all run
instead of being skipped:

```bash
python3 -m venv infra/aws-yolo/.venv
infra/aws-yolo/.venv/bin/pip install -r infra/aws-yolo/service/requirements.txt
infra/aws-yolo/.venv/bin/python -m unittest discover \
  -s infra/aws-yolo/tests -p 'test_*.py' -v
infra/aws-yolo/.venv/bin/python -m compileall -q \
  infra/aws-yolo/service infra/aws-yolo/tests
```

An end-to-end model test additionally needs the evaluated ONNX bundle and the packages
from `service/requirements.txt`. Terraform and Docker verification require those tools;
the repository does not emulate a successful AWS deployment when they are absent.
