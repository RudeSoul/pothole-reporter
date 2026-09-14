#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
STACK_NAME="${STACK_NAME:-pothole-reporter-central}"
AWS_REGION="${AWS_REGION:-ap-south-1}"
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-}"
CODE_KEY="${CODE_KEY:-releases/central-lambda.zip}"

command -v aws >/dev/null || { echo "AWS CLI is required; install it and run aws login first." >&2; exit 2; }
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "$AWS_REGION")"
if [[ -z "$ARTIFACT_BUCKET" ]]; then
  ARTIFACT_BUCKET="pothole-reporter-central-${ACCOUNT_ID}-${AWS_REGION}"
fi

cd "$ROOT_DIR"
npm install --prefix infra/aws-central --omit=dev --no-audit --no-fund
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
mkdir -p "$TMP_DIR/package/infra/aws-central" "$TMP_DIR/package/llm/generated"
cp -R infra/aws-central/service "$TMP_DIR/package/infra/aws-central/"
cp infra/aws-central/package.json "$TMP_DIR/package/infra/aws-central/"
cp -R infra/aws-central/node_modules "$TMP_DIR/package/infra/aws-central/"
cp llm/generated/contract.mjs "$TMP_DIR/package/llm/generated/"
(cd "$TMP_DIR/package" && zip -q -r "$TMP_DIR/central-lambda.zip" infra llm)

if ! aws s3api head-bucket --bucket "$ARTIFACT_BUCKET" --region "$AWS_REGION" >/dev/null 2>&1; then
  if [[ "$AWS_REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$ARTIFACT_BUCKET" --region "$AWS_REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$ARTIFACT_BUCKET" --region "$AWS_REGION" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null
  fi
  aws s3api put-bucket-versioning --bucket "$ARTIFACT_BUCKET" --versioning-configuration Status=Enabled --region "$AWS_REGION" >/dev/null
fi
aws s3 cp "$TMP_DIR/central-lambda.zip" "s3://$ARTIFACT_BUCKET/$CODE_KEY" --region "$AWS_REGION" >/dev/null

aws cloudformation deploy \
  --template-file infra/aws-central/template.yaml \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides CodeS3Bucket="$ARTIFACT_BUCKET" CodeS3Key="$CODE_KEY" \
  --no-fail-on-empty-changeset

aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs' --output table
