# Pothole Reporter central service

This directory is a standalone Cloudflare Worker. It moves tender selection,
cross-installation pothole deduplication, aggregate impact accounting, and a
provider-neutral shared-detection path behind one API. Users who select their own
OpenAI key still send vision requests directly to OpenAI; their key never reaches
this service.

The Worker uses:

- D1 for pseudonymous installations, canonical potholes, sightings, tenders, quotas,
  idempotency, and daily aggregate metrics.
- KV for short-lived replay protection.
- KGIS and Nominatim for server-side jurisdiction/address resolution. Client hints
  are used only when those sources do not return the corresponding field.
- A provider-neutral shared-detection seam: the
  [OpenAI Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
  by default, an operator-controlled HTTPS YOLO gateway, or OpenAI first with a
  narrowly classified YOLO fallback.
- OpenAI for final tender-candidate adjudication; that task is not delegated to a
  bounding-box detector.

All editable LLM prompts, schemas, model/detail/reasoning policy, request flags,
timeouts, and vision-input settings live in [`../llm/`](../llm/). The Worker imports
the generated contract; `npm test` and `npm run deploy` reject stale generated files.

The app sends image bytes only to `/v1/vision/detect`. They are held in request
memory, forwarded with `store: false`, and never written
to D1, KV, or Worker logs. Reports retain only a SHA-256 image digest. Exact
pothole coordinates are retained because they are the subject of the shared map.
Tender/report coordinates are sent to KGIS and Nominatim to resolve place data;
successful lookups have a bounded five-minute in-isolate cache. A successful tender
response, including its resolved address, is retained for idempotent replay for at most
24 hours and pruned by the scheduled Worker. Accepted pothole coordinates remain in the
canonical report and map records as described above.

## Provision once

Prerequisites are Node.js 22.5 or newer, a Cloudflare account, and Wrangler login.

```sh
cd server
npm install
npx wrangler login
npx wrangler d1 create pothole-reporter --location=apac
npx wrangler kv namespace create DEVICES
```

Copy the D1 `database_id` and KV `id` printed by those commands into the two
`REPLACE_WITH_...` placeholders in `wrangler.toml`. Also replace
`NOMINATIM_USER_AGENT` with an application name and monitored operator email or
website; the public Nominatim service requires an identifiable caller. A dedicated
geocoder should be configured before sustained production traffic.

Use the new `pothole-reporter` D1 database. The experimental
`origin/server-backed-v2` database was named `pothole` and used incompatible
`reports` and compact `tenders.tn/loc` tables; pointing this Worker at that ID and
running `CREATE TABLE IF NOT EXISTS` would not migrate those columns. Export and
transform that data separately if the experimental database contains records that
must be retained.

Create the schema locally and remotely:

```sh
npx wrangler d1 execute pothole-reporter --local --file=./schema.sql
npx wrangler d1 execute pothole-reporter --remote --file=./schema.sql
```

Set the production OpenAI project key as a Worker secret. Do not put it in
`wrangler.toml`.

```sh
npx wrangler secret put OPENAI_API_KEY
```

For local development only, copy `.dev.vars.example` to `.dev.vars` and replace
its placeholder. `.dev.vars` is ignored by Git.

## Shared detector backend

`SHARED_DETECTOR_PROVIDER` has three explicit, backward-compatible modes:

| Mode | Behaviour |
| --- | --- |
| `openai` | Default. Use the client-selected `gpt-5-mini` or `gpt-5.6`; requires `OPENAI_API_KEY`. |
| `http_yolo` | Send detection directly to the server-owned YOLO gateway. |
| `openai_then_http_yolo` | Try OpenAI, then call YOLO only after a confirmed non-retryable OpenAI credit/spend/usage exhaustion response. |

Configure the YOLO adapter with its exact regional API Gateway URL:

```text
YOLO_API_URL=https://<api-id>.execute-api.ap-south-1.amazonaws.com/v1/detect
YOLO_MODEL=your-versioned-model-name
YOLO_TIMEOUT_MS=30000
YOLO_AWS_REGION=ap-south-1
```

The Worker rejects HTTP, custom domains, query strings, alternate paths, and an
`execute-api` hostname whose region differs from `YOLO_AWS_REGION`. Set all caller
credentials as Worker secrets, never `[vars]` values:

```sh
npx wrangler secret put YOLO_AWS_ACCESS_KEY_ID
npx wrangler secret put YOLO_AWS_SECRET_ACCESS_KEY
# Required only when the credentials were issued by STS:
npx wrangler secret put YOLO_AWS_SESSION_TOKEN
npx wrangler secret put YOLO_API_KEY
```

Attach only the Terraform output `yolo_caller_policy_json` to that externally
managed caller principal. Prefer short-lived credentials where the Worker deployment
mechanism can rotate them. Terraform deliberately creates neither the caller nor an
access key.

Every YOLO request is AWS SigV4-signed for the regional `execute-api` service. Because
SigV4 owns `Authorization`, the separate project gateway token is sent in the signed
`X-Yolo-API-Key` header and hash-verified again inside Lambda. Both shared YOLO modes
require both IAM credentials and this second token.

After the AWS endpoint and secret exist, enable the requested primary/fallback chain:

```text
SHARED_DETECTOR_PROVIDER=openai_then_http_yolo
```

The chain falls back only when a `429` response contains one of the currently
[documented OpenAI exhaustion codes](https://developers.openai.com/api/docs/guides/error-codes):
`credit_balance_exhausted`, `organization_spend_limit_exceeded`,
`project_spend_limit_exceeded`, or `organization_usage_limit_exceeded`. It does
not fall back for an ordinary request/token rate limit, `slow_down`, the broad
`insufficient_quota` type by itself, an ambiguous legacy `insufficient_quota`
code, timeout/network failure, authentication/permission failure, malformed
request, unreadable/invalid model output, or OpenAI 5xx. Those failures must be
retried or repaired according to their actual cause; silently changing models
could otherwise hide an outage or configuration error.

The server sends exactly one data-URL image plus capture mode, language, model,
prompt/schema versions, and `X-Request-ID`. The gateway must return the complete
five-field road-damage v4 verdict directly or under `verdict`/`result`; raw boxes and
classes need a thin normalizing adapter. Responses retain
`detector.provider="shared_server"` for
released clients and add the actual `detector.backend_provider` for provenance.
Successful fallback responses also contain `fallback_from="openai"` and
`fallback_reason="openai_exhausted"`. The same server request ID is sent to YOLO;
structured logs retain that ID, the OpenAI and YOLO upstream IDs, and the exact
exhaustion code, without logging images or coordinates.

One accepted `/v1/vision/detect` operation consumes exactly one set of central
D1 quota counters even when it makes both an OpenAI and YOLO call. A successful
result is cached under the original idempotency key, so replay calls neither
provider and consumes no additional quota. A failed fallback is not cached and
its idempotency lease is released; an explicit retry is a new billable attempt
and consumes one additional quota unit. The AWS gateway should enforce its own
hard request/spend admission cap as a second, independent boundary.

When the AWS gateway returns HTTP 429 JSON with exactly
`monthly_request_cap_exceeded` or
`monthly_estimated_budget_cap_exceeded`, the Worker returns
`shared_yolo_cap_reached` with `details.retryable=false`, preserves the
`yolo_error_code` and `retry_after_seconds`, and opens a global circuit for that
configured YOLO model until the advertised reset time. The circuit is a small
`DEVICES` KV value containing only the code and cap-until timestamp; it contains
no request, image, installation, or location data. Later fallback attempts still
try the OpenAI primary first, because it may have recovered, but avoid another AWS
call while the YOLO circuit is active. Other 429 codes remain retryable capacity
failures and never open this circuit.

YOLO replaces road-damage detection only. Tender candidate adjudication still requires
`OPENAI_API_KEY`. The health endpoint reports
detection mode plus primary/fallback provider, model, and configuration status;
the older `shared_vision_provider`, `shared_vision_model`, and
`shared_vision_configured` fields remain for released clients.

## Complaint boundary

This Worker does not file complaints with GBA, BBMP, Sahaaya, or any other authority.
The app's sole complaint action is a user-controlled, prefilled email draft routed to
the commissioner or responsible authority. Location and pothole details are always
included; a probable tender number is included only when resolution found one. See
[the government complaint boundary](../docs/GBA_INTEGRATION.md).

## Import tenders

The checked-in Karnataka export uses compact fields (`tn`, `t`, `loc`, `c`, `d`,
`b`). The importer accepts those or the expanded database names. It deliberately
skips rows without `b`/`body_lgd`: without an awarding-body key, the server cannot
safely claim that a contract belongs to the detected jurisdiction.

Generate an auditable SQL file, inspect its summary on stderr, then apply it:

```sh
node ./tools/import-tenders.mjs ../data/tenders-karnataka.json \
  "Karnataka public tender export" "REPLACE_WITH_AUTHORITATIVE_SOURCE_URL" \
  > tenders.generated.sql
npx wrangler d1 execute pothole-reporter --local --file=./tenders.generated.sql
npx wrangler d1 execute pothole-reporter --remote --file=./tenders.generated.sql
rm tenders.generated.sql
```

`REPLACE_WITH_AUTHORITATIVE_SOURCE_URL` must be changed to the actual source page
used for that refresh. The import is repeatable: tender numbers are upserted.

## Develop, test, and deploy

```sh
npm test
npm run dev
npm run deploy
```

The Worker name remains `pothole-detect`, matching the checked-in default client
URL. If the Cloudflare account or route differs, set the client `service_url` to
the deployed URL. Smoke-test `GET /v1/health`, register a fresh test installation,
and check the public `/map`. Follow production request IDs with:

```sh
npx wrangler tail --format pretty
```

The default quota knobs in `wrangler.toml` are 200 shared-vision calls per
installation per UTC day, 120 shared calls globally per UTC minute, 5,000
globally per UTC day, and 50,000 globally per UTC month. Tune them to the project
budget before deployment.
The global limits are D1-atomic and apply across installations without retaining
IP addresses. `OPENAI_TIMEOUT_MS` defaults to and is capped at 55 seconds;
`YOLO_TIMEOUT_MS` defaults to and is capped at 30 seconds, leaving Worker-side
headroom over the AWS gateway's 29-second API timeout. Fallback mode can use both
budgets sequentially, so browser and native shared-vision calls use a 100-second
deadline: 85 seconds for upstreams plus 15 seconds for Worker and network overhead.
An absent/invalid OpenAI secret, an ordinary rate limit, and confirmed credit/limit
exhaustion return distinct `shared_*` errors. Tender resolution converts provider
failures into non-cached HTTP 503 responses with `details.retryable: true`.

## Request identity and signing

Every response has a server-generated UUID in both `X-Request-ID` and JSON
`request_id` (except the empty OPTIONS body). Structured logs contain that ID,
route, status, aggregate outcome/vision mode, optional pothole ID, actual detector
backend, optional OpenAI and YOLO upstream request IDs, and any classified OpenAI
exhaustion or YOLO monthly-cap code. They do not contain coordinates, images, request
bodies, signing keys, or a stable installation identifier.

Register once:

```text
POST /v1/installations
Content-Type: application/json

{"public_key":"BASE64_P256_PUBLIC_KEY"}
```

The key may be a 65-byte uncompressed raw P-256 public key or SPKI DER. The
response is `201 {request_id, install_id}`; `install_id` is a pseudonymous digest
of the public key.

Every other POST is signed with:

```text
X-Install-ID: <install_id>
X-Timestamp: <Unix epoch milliseconds>
X-Signature: <base64 ECDSA-SHA256 signature>
Idempotency-Key: <stable operation UUID>
```

Sign these UTF-8 bytes exactly, joined by four LF (`\n`) separators and with no
trailing LF:

```text
UPPERCASE_METHOD
URL_PATHNAME_ONLY
X_TIMESTAMP
IDEMPOTENCY_KEY_OR_EMPTY
LOWERCASE_SHA256_HEX_OF_EXACT_BODY_BYTES
```

For example, the pathname is `/v1/potholes/report`; query parameters and origin are
not signed. Timestamps outside five minutes are rejected. Browser
WebCrypto's 64-byte P1363 `r || s` signature and Android's ASN.1 DER ECDSA
signature are both accepted. Every signed POST requires `Idempotency-Key`,
including tender resolution.

The first request for an installation/route/key/body obtains an atomic D1 lease
before mutations, quota charging, or OpenAI calls. A simultaneous duplicate gets
`425 idempotency_in_progress` with `details.retryable: true`; retrying the same
body and key after the owner finishes returns the cached result. Completion writes
the response and removes the lease in one D1 batch. Errors remove the lease and
are not cached; an abandoned lease may be reclaimed after three minutes
(`IDEMPOTENCY_CLAIM_TTL_MS`). That safe three-minute baseline cannot be configured
lower because a provider timeout plus geolocation can exceed one minute.

Errors use one stable envelope:

```json
{"request_id":"...","error":"machine_code","message":"Human explanation","details":{}}
```

`details` is omitted when there is no safe structured detail.

## API contract

### Vision

`POST /v1/vision/detect` accepts `images` containing exactly one
`{"data_url":"data:image/...;base64,..."}` object, `capture_mode` (`manual` or
`drive`), `language` (`en` or `kn`), `model` (`gpt-5-mini` or `gpt-5.6`), and
`image_detail` (`high`, or `original` only with `gpt-5.6`), and
`prompt_version: "road-damage-v5"`. `image_detail` defaults to `high`. A pure
YOLO deployment accepts only the universal `high` setting and does not forward
the OpenAI-only detail field to the YOLO gateway. An OpenAI-primary deployment
uses the requested detail; if it falls back to YOLO, the gateway request omits it.
The endpoint returns the flat v4 verdict plus `detector`, per-installation `quota`,
and `request_id`. The verdict contains exactly these fields:

- `image_quality`: `acceptable` or `rejected`.
- `assessment`: `damaged` or `undamaged`.
- `damage_type`: `pothole_cavity`, `failed_patch`, `surface_breakup`,
  `rut_or_depression`, `other_road_damage`, or `null`.
- `size`: `small`, `medium`, `large`, or `null`.
- `description`: a short factual explanation.

Asphalt, concrete, gravel, dirt, and mud roads are all in scope. Visible road damage
at an edge remains damage even when it touches a kerb, gutter, shoulder, drain
surround, or road-to-footpath joint. Damage confined to an intact non-road object is
not road damage.

JPEG, PNG, and WebP magic bytes are checked. Each decoded image is limited to
3.5 MB; OpenAI-only detection is limited to 8 MB decoded total, while YOLO/direct
or OpenAI-with-YOLO-fallback detection is limited to 4 MB so base64 JSON plus the
API Gateway proxy envelope remains below Lambda's tighter 6 MB synchronous payload
ceiling. The whole Worker JSON body is limited to 17 MB.

### Personal-key activity

Personal-key calls do not otherwise reach this server when the model finds no
damage. After every personal OpenAI vision attempt, send only:

```json
{"event":"vision_check","vision_provider":"personal_openai","capture_mode":"manual"}
```

`capture_mode` may also be `drive`; extra fields (including coordinates) are
rejected. `POST /v1/activity` returns
`202 {request_id, accepted: true, event: "vision_check"}` and records only an
aggregate own-key check. Do not send it for shared-server vision calls, which are
already counted by `/v1/vision/detect`.

### Tender resolution

`POST /v1/tenders/resolve` accepts
`{lat, lng, address_hint?, lgd_hint?, town_hint?}`. The server first requests an
address from Nominatim and the urban-local-body boundary from KGIS, falling back
field-by-field to hints. It limits candidates to that body's tenders (the five
Bengaluru successor corporations also see the legacy `BLR` pool), ranks a
deterministic shortlist, and asks the server model to adjudicate it. The stable
matching policy is sent as top-level Responses API instructions. The
reverse-geocoded address and shortlisted database rows are sent separately as a
delimited, untrusted JSON document, with an explicit instruction to use text
inside either source only as evidence and not as model instructions.

The adjudicator must reject contracts whose scope is only a footpath, sidewalk,
pedestrian walkway, kerb, drain, culvert, utility, landscaping, building, park, or
other non-road-surface work, even when the street, locality, or ward matches exactly.
A combined work remains eligible only when its own text explicitly includes roadway
resurfacing, pavement or road repair, pothole filling, rehabilitation, or road
maintenance; a generic phrase such as “improvement work” is not enough.

The response is `{request_id, jurisdiction, tender, reason}`. `tender` is null
when jurisdiction, address, candidates, or confidence is insufficient. A null
result is safer than naming an unrelated contractor. Shared-model credit,
configuration, quota, and availability failures are non-cached HTTP 503 errors
with their existing `shared_*`/`daily_vision_limit` code and
`details.retryable: true`. A
genuine out-of-coverage or no-match result is HTTP 200 and terminal. If a needed
KGIS or Nominatim lookup is temporarily unavailable and no supplied hint fills
that field, the endpoint instead returns HTTP 503
`{error:"geolocation_unavailable", details:{retryable:true, services:[...]}}` so
mobile background work retries rather than permanently accepting an empty result.
Retryable 503 responses are not persisted, so retry the exact body with the same
idempotency key. Every terminal HTTP 200 tender result—including a safe null
result—is persisted; replaying it returns `idempotent_replay: true` without
spending another geolocation or shared-model call.

Before releasing a prompt or confidence-threshold change, run
`python3 ../eval/run_tender_eval.py`. The curated project-policy cases include real
road-work matches and exact-locality non-road negatives. The harness shuffles candidate
order and reports TP/FP/FN/TN, precision, recall, F1, wrong-selection counts, and a
threshold sweep. Its release gate requires both precision and recall, so returning null
for every case cannot pass. Treat it as a regression suite, not as a sealed or
population-representative accuracy estimate.

After a successful authoritative lookup, the endpoint also backfills `lgd` and
`town` on the nearest canonical pothole within the dedupe radius that the signed
installation previously observed. This repairs records first uploaded while the
location providers were unavailable without accepting arbitrary map edits.

### Reports and deduplication

`POST /v1/potholes/report` accepts:

```json
{
  "client_observation_id": "device-generated-uuid",
  "observed_at": 1788500000000,
  "lat": 12.9115,
  "lng": 77.6427,
  "gps_accuracy_m": 5,
  "heading_deg": 90,
  "speed_mps": 8,
  "damage_type": "pothole_cavity",
  "size": "medium",
  "image_hash": "64-lowercase-hex-characters",
  "detector": {
    "provider": "personal_openai",
    "model": "gpt-5-mini",
    "prompt_version": "road-damage-v5",
    "schema_version": 4
  },
  "lgd_hint": "305852",
  "town_hint": "Bengaluru South City Corporation"
}
```

The response is `201` for a new canonical pothole and `200` for a duplicate:
`{request_id, duplicate, resubmitted?, dedupe, pothole}`. A distinct installation
increments `pothole.seen_count` once; repeated sightings remain audit records but
do not inflate that impact count. Personal provider `personal_openai` (and legacy
`own_key`) is normalized to aggregate metric `own_key`.

The report endpoint independently resolves its coordinates through KGIS and
Nominatim before writing the canonical `lgd`/`town`; this remains correct when a
native drive report arrives before its tender request. Supplied jurisdiction hints
are field-level outage fallbacks, not authoritative map data.

The dedupe search uses a 12 m base radius, expanded up to 20 m by reported GPS
accuracy, compatible damage families/sizes, and a 120-day observation horizon.
Nearby compatible observations dedupe against the canonical pothole regardless of
any lifecycle fields retained in an older database. The current product neither asks
people to classify repairs nor changes a canonical record to fixed.

Observation insertion, observer upsert, exact `first_seen_at`/`last_seen_at` and
distinct-observer count recomputation, and authoritative jurisdiction enrichment
commit in one transactional D1 batch. A same-observation resubmission reruns those
idempotent projections from
the stored observations, so it can repair rows created by older partial-write
deployments without duplicating the sighting or count. New canonical creation
must precede the lower-ID concurrency reconciliation; if its first-observation
batch fails, the Worker guard-deletes that canonical only while it remains
unobserved. If another concurrent report has already adopted it, the guards keep
the row and its stored facts are reconciled on resubmission.

### Public impact

- `GET /v1/map?bbox=west,south,east,north&since=<ms>&limit=1000`
  returns `{request_id, type:"FeatureCollection", total, features}`. The default
  window is 180 days and maximum result count is 2,000.
- `GET /v1/impact?from=YYYY-MM-DD&to=YYYY-MM-DD` returns aggregate requests,
  active installations, new potholes, observations, and distinct observers. The
  default window is the last 30 UTC days.
- `GET /map` is the public Leaflet dashboard backed by those two endpoints.
- `GET /v1/health` exposes configuration/prompt versions but no secret.

Neither public endpoint exposes installation IDs or individual request history.

## Trust limits

The public-key registration is self-service. Its signature proves that later
requests came from the same pseudonymous installation; it does **not** prove the
software is the genuine app or that one installation equals one person. A
determined caller can mint many keypairs, so report and activity totals must be
described as requests and participating installations, never verified people.
The global minute/day/month D1 caps bound shared-inference exposure even when an
attacker mints installations, but they can still deny service by consuming that
shared allowance. They do not establish identity or genuine-app provenance.

Before a high-profile public launch, add Cloudflare rate limiting/WAF rules and
platform attestation such as Play Integrity, and establish a moderation/correction
workflow. Attestation is intentionally not represented as implemented in this
repository.
