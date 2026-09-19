#!/usr/bin/env bash
# Run the packaged app on real phones in AWS Device Farm and report crashes.
#
#   AWS_PROFILE=pothole tools/harness/devicefarm-run.sh [path/to.apk]
#
# The emulator smoke test proves one Android version on one virtual device. This runs
# the same APK on the Top Devices pool (recent Pixels, Samsungs, a tablet) and fails if
# any of them crash, hang or refuse to install. It uses Device Farm's built-in fuzz
# test, which needs no test code: it installs the app, drives random input, and reports
# crashes with logs and screenshots.
#
# Cost: device-minutes. The account's free trial covers a run of this size; check
# `aws devicefarm get-account-settings` before running it repeatedly.
set -euo pipefail
cd "$(dirname "$0")/../.."

REGION="${DEVICEFARM_REGION:-us-west-2}"
PROJECT_NAME="${DEVICEFARM_PROJECT:-pothole-reporter}"
APK="${1:-android-app/android/app/build/outputs/apk/debug/app-debug.apk}"
EVENTS="${DEVICEFARM_EVENTS:-200}"
[ -s "$APK" ] || { echo "FAIL no APK at $APK"; exit 2; }

project="$(aws devicefarm list-projects --region "$REGION" \
  --query "projects[?name=='$PROJECT_NAME'].arn | [0]" --output text)"
if [ "$project" = "None" ] || [ -z "$project" ]; then
  project="$(aws devicefarm create-project --region "$REGION" --name "$PROJECT_NAME" \
    --query "project.arn" --output text)"
fi
pool="$(aws devicefarm list-device-pools --region "$REGION" --arn "$project" \
  --query "devicePools[?name=='Top Devices'].arn | [0]" --output text)"

echo "1/4 uploading $(basename "$APK")"
upload="$(aws devicefarm create-upload --region "$REGION" --project-arn "$project" \
  --name "$(basename "$APK")" --type ANDROID_APP --output json)"
upload_arn="$(echo "$upload" | python3 -c 'import json,sys;print(json.load(sys.stdin)["upload"]["arn"])')"
upload_url="$(echo "$upload" | python3 -c 'import json,sys;print(json.load(sys.stdin)["upload"]["url"])')"
curl -s -T "$APK" "$upload_url" >/dev/null

echo "2/4 waiting for the upload to validate"
for _ in $(seq 1 40); do
  status="$(aws devicefarm get-upload --region "$REGION" --arn "$upload_arn" \
    --query "upload.status" --output text)"
  [ "$status" = "SUCCEEDED" ] && break
  [ "$status" = "FAILED" ] && { echo "FAIL Device Farm rejected the APK"; exit 1; }
  sleep 5
done
[ "$status" = "SUCCEEDED" ] || { echo "FAIL upload did not validate"; exit 1; }

echo "3/4 running on the Top Devices pool"
run="$(aws devicefarm schedule-run --region "$REGION" --project-arn "$project" \
  --app-arn "$upload_arn" --device-pool-arn "$pool" \
  --name "$(git rev-parse --short HEAD) smoke" \
  --test "{\"type\":\"BUILTIN_FUZZ\",\"parameters\":{\"event_count\":\"$EVENTS\",\"throttle\":\"100\"}}" \
  --execution-configuration '{"jobTimeoutMinutes":10}' \
  --query "run.arn" --output text)"

echo "4/4 waiting for results (a few minutes)"
while true; do
  state="$(aws devicefarm get-run --region "$REGION" --arn "$run" --query "run.status" --output text)"
  [ "$state" = "COMPLETED" ] && break
  sleep 30
done

aws devicefarm list-jobs --region "$REGION" --arn "$run" \
  --query "jobs[].{device:device.name,result:result}" --output text | sed 's/^/  /'
result="$(aws devicefarm get-run --region "$REGION" --arn "$run" --query "run.result" --output text)"
minutes="$(aws devicefarm get-run --region "$REGION" --arn "$run" \
  --query "run.deviceMinutes.total" --output text)"
echo "device minutes used: $minutes"
[ "$result" = "PASSED" ] || { echo "FAIL Device Farm run result: $result"; exit 1; }
echo "device farm run passed on every device in the pool"
