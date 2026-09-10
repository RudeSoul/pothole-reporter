# Pothole Reporter central service

This directory is a standalone Cloudflare Worker. It moves tender selection,
cross-installation pothole deduplication, aggregate impact accounting, and a
provider-neutral shared-detection path behind one API. A person does not need a
personal OpenAI key when the operator has configured shared detection: the app can
register a pseudonymous signing key, call `/v1/vision/detect`, receive a detection
receipt, submit the corresponding observation, resolve a probable municipal tender,
and read the shared map through this service. This is operator-funded, best-effort
capacity, not unlimited inference. Users who select their own OpenAI key still send
vision requests directly to OpenAI; their key never reaches this service.

The Worker uses:

- D1 for pseudonymous installations, canonical potholes, sightings, tenders, quotas,
  shared-detection receipts, idempotency, and daily metrics.
- KV for short-lived replay protection.
- KGIS Town, National/State/District Highway, and Gram Panchayat layers for
  server-side road-ownership gates, plus an operator-configured Nominatim-compatible reverse
  geocoder. Client hints never override or bypass the tender ownership gate.
- A provider-neutral shared-detection seam: the
  [OpenAI Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
  by default, an operator-controlled HTTPS YOLO gateway, or OpenAI first with a
  narrowly classified YOLO fallback.
- OpenAI for preferred tender-candidate adjudication, with a strict deterministic
  location/scope fallback; that task is never delegated to a bounding-box detector.

All editable LLM prompts, schemas, model/detail/reasoning policy, request flags,
timeouts, and vision-input settings live in [`../llm/`](../llm/). The Worker imports
the generated contract; `npm test` and `npm run deploy` reject stale generated files.

The app sends image bytes to the server only through `/v1/vision/detect`. They are
held in request memory, forwarded with `store: false` when OpenAI is used, and are
never written to D1, KV, or Worker logs. D1 retains the SHA-256 image digest and,
for an eligible shared result, a receipt bound to that digest and verdict. Exact
pothole coordinates are retained because they are the subject of the shared map.
Tender/report coordinates are sent to KGIS Town and the National, State, and
District Highway layers using a bounded phone-GPS proximity check; they are sent to Gram
Panchayat when the Town/highway layers contain no feature, and, when configured, to
the reverse geocoder. Complete successful lookups have a bounded five-minute
in-isolate cache. See [Privacy and retention](#privacy-and-retention) for the
database and replay windows.

## Provision once

Prerequisites are Node.js 22.5 or newer, a Cloudflare account, Wrangler login, a D1
database, and a KV namespace. A useful no-personal-key deployment additionally needs:

- one configured shared detector (`openai`, `http_yolo`, or the chained mode);
- working access to all five fixed KGIS HTTPS query endpoints (Town, National,
  State and District Highway, and Gram Panchayat);
- an operator-owned Nominatim-compatible HTTPS reverse geocoder for sustained use,
  or an explicit low-volume public-Nominatim opt-in;
- an imported, authoritative tender dataset with `body_lgd` ownership keys if
  tender attribution is expected; and
- explicit quotas, receipt-enforcement rollout state, and an app `service_url` that
  points to this deployment.

The repository contains code and placeholder configuration only. It does not prove
that the checked-in default URL is deployed, migrated, funded, populated, or healthy.

```sh
cd server
npm install
npx wrangler login
npx wrangler d1 create pothole-reporter --location=apac
npx wrangler kv namespace create DEVICES
```

Copy the D1 `database_id` and KV `id` printed by those commands into the two
`REPLACE_WITH_...` placeholders in `wrangler.toml`.

Use the new `pothole-reporter` D1 database. The experimental
`origin/server-backed-v2` database was named `pothole` and used incompatible
`reports` and compact `tenders.tn/loc` tables; pointing this Worker at that ID and
running `CREATE TABLE IF NOT EXISTS` would not migrate those columns. Export and
transform that data separately if the experimental database contains records that
must be retained.

For a fresh database, create the schema locally and remotely:

```sh
npx wrangler d1 execute pothole-reporter --local --file=./schema.sql
npx wrangler d1 execute pothole-reporter --remote --file=./schema.sql
```

Do not run `upgrade-v1-receipts.sql` on a fresh database. For a database that was
already created from this service's pre-receipt `schema.sql`, take an operator
backup, stop writes or otherwise arrange a controlled rollout, and apply this
non-idempotent migration exactly once **before** deploying code that reads the new
columns and tables:

```sh
npx wrangler d1 execute pothole-reporter --local \
  --file=./upgrade-v1-receipts.sql
npx wrangler d1 execute pothole-reporter --remote \
  --file=./upgrade-v1-receipts.sql
```

The migration adds `observations.verification_state`,
`shared_detection_receipts`, and `external_rate_gates`. Existing observations are
conservatively labelled `client_attested`; the migration cannot retroactively prove
that the server saw their images. It does not transform the incompatible
experimental `pothole` database described above and it does not import tenders.

### Reverse geocoder

For sustained traffic, set `GEOCODER_REVERSE_URL` to the operator's self-hosted or
contracted Nominatim-compatible `/reverse` HTTPS endpoint and set an identifiable
`GEOCODER_USER_AGENT`. Embedded URL credentials, plaintext HTTP, and redirects are
rejected. If the service uses bearer authentication, store it as a secret:

```sh
npx wrangler secret put GEOCODER_BEARER_TOKEN
```

`ALLOW_PUBLIC_NOMINATIM` defaults to `false`. When `GEOCODER_REVERSE_URL` is empty,
setting the flag to `true` selects
`https://nominatim.openstreetmap.org/reverse`; explicitly configuring that public
hostname also requires the flag. It is not an automatic failover when an operator
endpoint fails, and a configured operator endpoint takes precedence. Public requests
are admitted through a D1-wide one-request-per-second lease, rejected lease attempts
are treated as temporary geocoder unavailability, and the bearer token is never sent
to the public host. This opt-in is a development or genuinely low-volume fallback,
not a sustained-production plan.

Municipal tender attribution fails closed when the geocoder is absent or unavailable.
An `address_hint` does not bypass that gate. `/v1/health` reports configuration state,
not a live geocoder or KGIS probe.

### Server OpenAI key

Set `OPENAI_API_KEY` as a Worker secret when shared detection uses the default
`openai` mode, to make OpenAI the preferred detector in
`openai_then_http_yolo`, and to enable preferred model adjudication of tender
candidates. Never put it in `wrangler.toml`.

```sh
npx wrangler secret put OPENAI_API_KEY
```

Pure `http_yolo` detection can run without `OPENAI_API_KEY`. Chained mode also runs
its fully configured YOLO backend directly when the OpenAI key is absent; it starts
with OpenAI whenever that key is configured and switches only for the documented
exhaustion response. A direct no-key YOLO response identifies `http_yolo` as its
backend and does not claim that an OpenAI request failed. Without the key, tender
resolution uses the deliberately narrow deterministic location/scope matcher; it
does not send tender selection to YOLO. An invalid configured key is an operator
error rather than a signal to silently change matching strategy.

For local development only, copy `.dev.vars.example` to `.dev.vars` and replace or
remove its placeholder to match the mode being tested. `.dev.vars` is ignored by Git.

## Shared detector backend

`SHARED_DETECTOR_PROVIDER` has three explicit, backward-compatible modes:

| Mode | Behaviour |
| --- | --- |
| `openai` | Default. Use the client-selected `gpt-5-mini` or `gpt-5.6`; requires `OPENAI_API_KEY`. |
| `http_yolo` | Send detection directly to the server-owned YOLO gateway. |
| `openai_then_http_yolo` | Use OpenAI first when its key is configured, then YOLO only after confirmed credit/spend/usage exhaustion; with no OpenAI key, use the fully configured YOLO backend directly. |

YOLO is optional and external to this Worker; no model binary or live gateway is
created by the server package. `http_yolo` and `openai_then_http_yolo` fail closed
until the evaluated gateway URL, IAM caller credentials, and project gateway token
are all configured.

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

YOLO replaces road-damage detection only. Tender candidate adjudication prefers
the server's `OPENAI_API_KEY`. When that key is absent, confirmed exhausted, or
blocked by the per-installation daily or project-wide daily/monthly cap, the server
applies a deliberately narrow deterministic fallback: exactly one location-
shortlisted candidate must explicitly name road-surface work. Ambiguous,
footpath/drain-only, and wrong-locality pools return no match rather than guessing a
contractor. The global per-minute gate, ordinary upstream rate limits, network
failures, invalid credentials, and provider errors remain retryable failures. The
health endpoint reports detection mode plus primary/fallback provider, model, and
configuration status; the older `shared_vision_provider`, `shared_vision_model`, and
`shared_vision_configured` fields remain for released clients.

## Complaint boundary

This Worker does not file complaints with GBA, BBMP, Sahaaya, or any other authority.
The app's sole complaint action is a user-controlled, prefilled email draft routed to
the commissioner or responsible authority. Location and pothole details are always
included; a probable tender number is included only when resolution found one. See
[the government complaint boundary](../docs/GBA_INTEGRATION.md).

## Import tenders

`schema.sql` creates an empty `tenders` table. Importing scoped source data is a
deployment requirement for useful tender matching; without it, detection, reporting,
deduplication, and the public map still work, but `/v1/tenders/resolve` terminates
with `tender: null` and `reason: "no_tenders_for_jurisdiction"` for otherwise valid
municipal requests.

The checked-in Karnataka export uses compact fields (`tn`, `t`, `loc`, `c`, `d`,
`b`). The importer accepts those or the expanded database names. It deliberately
skips rows without `b`/`body_lgd`: without an awarding-body key, the server cannot
safely claim that a contract belongs to the detected jurisdiction.

Current checked-in data is useful but not statewide contractor coverage: it has
42,283 rows, of which 13,577 have a safe body key. Only 1,121 rows have both a body
key and nonblank contractor, and all 1,121 are in the legacy Bengaluru `BLR` pool.
Elsewhere, the endpoint can still return a probable tender number/title when a safe
match exists, but `contractor` remains null unless the import actually contains it;
the server never invents a winning bidder.

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
used for that refresh. Inspect the prepared/skipped counts printed on stderr before
applying the SQL. The import is repeatable: tender numbers are upserted. Confirm that
the target database is populated rather than assuming a successful schema migration
also loaded data:

```sh
npx wrangler d1 execute pothole-reporter --remote \
  --command="SELECT COUNT(*) AS scoped_tenders FROM tenders WHERE body_lgd IS NOT NULL"
```

An imported publication record can support only a probable match. It does not prove
current maintenance responsibility, award status, completion, or a defect-liability
period; API results retain `warranty_code: "unverified"`.

## Receipt migration and compatible rollout

`REQUIRE_SHARED_DETECTION_RECEIPT=true` is the secure default in `wrangler.toml`.
With it enabled, a report claiming `detector.provider="shared_server"` is accepted
only with a valid server-issued receipt. A client must therefore send
`client_observation_id`, `lat`, and `lng` with the detection request, retain the
returned receipt, and include it with the matching report.

Older released clients may perform shared detection without those receipt fields. On
an existing service, use this rollout order:

1. Apply `upgrade-v1-receipts.sql` exactly once.
2. Initially deploy with `REQUIRE_SHARED_DETECTION_RECEIPT=false` if old clients must
   remain able to report.
3. Release receipt-capable browser and Android clients and verify their reports are
   counted as `server_verified_shared`.
4. Change the flag to `true`, deploy again, and confirm
   `shared_detection_receipts_required: true` on `/v1/health`.

The compatibility setting affects only a missing receipt. When it is `false`, a
receipt supplied by a new client is still validated; a legacy receipt-less shared
report is stored as `client_attested`, never upgraded by assertion alone, and must
still use the currently deployed detector prompt/schema contract. Do not leave the
flag off and describe all shared reports as server-verified.

### Tender ownership-policy replay rollout

The public endpoint and client-supplied idempotency key remain unchanged, but the
current road-ownership policy stores tender results under the internal D1 route
`/v1/tenders/resolve@ownership-v2`. Rows and in-flight claims written by older
deployments under `/v1/tenders/resolve` are deliberately ignored. Consequently, the
first retry after this deployment recomputes ownership and tender attribution once;
an old municipal/contractor response can neither replay nor cause an idempotency-hash
conflict. Later retries replay the newly computed `ownership-v2` result normally.

This namespace change needs no schema migration and supports old app clients without
changing their request. The scheduled retention cleanup covers both the legacy and
current result namespaces. After taking a database backup, an operator may also remove
legacy rows and abandoned claims immediately; correctness does not depend on doing so:

```sql
DELETE FROM idempotency_keys WHERE route = '/v1/tenders/resolve';
DELETE FROM idempotency_claims WHERE route = '/v1/tenders/resolve';
```

Any future change that alters the meaning or safety boundary of a cached tender result
must bump `TENDER_IDEMPOTENCY_ROUTE` again and add that namespace to scheduled cleanup.
Changing only the public route handler while reusing an old storage namespace is not a
safe rollout.

## Develop, test, and deploy

```sh
npm test
npm run dev
```

`npm test` uses in-memory D1/KV and mocked upstream responses. It verifies the API
contract, gates, receipts, idempotency, provider fallbacks, and counters, but it is
not a live OpenAI, YOLO, KGIS, geocoder, Cloudflare binding, or tender-data test.

Deploy only after replacing both binding placeholders, applying the correct schema
path, importing tenders, setting secrets, selecting the receipt rollout flag, and
tuning the caps:

```sh
npm run deploy
```

The Worker name remains `pothole-detect`, matching the checked-in default client
URL. If the Cloudflare account or route differs, set the client `service_url` to
the deployed URL. Neither that name nor the checked-in URL is evidence of a live
deployment.

For a local or operator-owned staging URL, basic read-only smoke checks are:

```sh
SERVICE_URL=https://REPLACE_WITH_OPERATOR_DEPLOYMENT
curl -fsS "$SERVICE_URL/v1/health"
curl -fsS "$SERVICE_URL/v1/impact"
curl -fsS -o /dev/null "$SERVICE_URL/map"
```

Treat `/v1/health` honestly: `ok: true` proves that the route executed, while its
booleans describe configuration only. It does not probe D1/KV writes, count imported
tenders, call KGIS or the geocoder, spend an OpenAI request, or invoke YOLO. Before
rollout, use a fresh staging installation in the actual app (or an equivalent signed
P-256 client) to complete all of these operations:

1. Run a shared detection with `client_observation_id`, `lat`, and `lng`; require a
   damaged/acceptable test result to return `detection_receipt` and an expiry.
2. Submit that exact observation and receipt; verify the report succeeds and the map
   exposes `verification: "server_verified_shared"`.
3. Resolve a known supported municipal coordinate whose `body_lgd` has imported
   tenders, and separately verify a known National Highway returns no tender.
4. Exercise the configured provider path: OpenAI primary, direct YOLO, or—only when
   staging has an operator-controlled fault injection—a documented-exhaustion
   response for the chained fallback. A normal 429 must not be treated as fallback
   success.

These are real upstream smoke tests and may consume quota. Unsigned `curl` calls to a
POST route prove only that authentication rejects them. Follow a returned request ID
in Worker logs with:

```sh
npx wrangler tail --format pretty
```

The default quota knobs in `wrangler.toml` are `DAILY_VISION_CAP=200` shared-model
operations per installation per UTC day, `GLOBAL_VISION_MINUTE_CAP=120`,
`GLOBAL_VISION_DAILY_CAP=5000`, and `MONTHLY_VISION_CAP=50000`. A model-adjudicated
tender lookup uses the same admission counters as a shared detection; a no-key
deterministic tender lookup does not call a provider. Tune all limits to the project
budget before deployment.
Setting any configured cap to `0` is a fail-closed kill switch; it never means
unlimited. A request refused by a later cap rolls back every earlier quota counter,
so overload retries do not consume an installation's daily allowance.
The global limits are D1-atomic and apply across installations without retaining
IP addresses. `OPENAI_TIMEOUT_MS` defaults to and is capped at 55 seconds;
`YOLO_TIMEOUT_MS` defaults to and is capped at 30 seconds, leaving Worker-side
headroom over the AWS gateway's 29-second API timeout. Fallback mode can use both
budgets sequentially, so browser and native shared-vision calls use a 100-second
deadline: 85 seconds for upstreams plus 15 seconds for Worker and network overhead.
For shared OpenAI detection, an absent/invalid secret, an ordinary rate limit, and
confirmed credit/limit exhaustion return distinct `shared_*` errors. Tender
resolution converts retryable model-provider failures into non-cached HTTP 503
responses with `details.retryable: true`; its no-key and eligible-cap fallbacks are
the deterministic cases described above.

`KGIS_NH_PROXIMITY_METRES` defaults to 20 and is clamped to 5–50 metres for all
three highway-polygon checks. KGIS supports the ArcGIS `distance` query used here;
the bounded buffer prevents ordinary phone-GPS drift from silently classifying a
highway as municipal. Raising it reduces missed highway classifications but can
conservatively suppress attribution for a closely parallel municipal road.

## Request identity and signing

Every response, including HTML and the empty OPTIONS response, has a server-generated
UUID in `X-Request-ID`; every JSON response carries the same value as `request_id`.
The Worker forwards it as `X-Client-Request-ID` to OpenAI and `X-Request-ID` to YOLO.
Structured logs contain that ID, route, status, aggregate outcome/vision mode,
optional pothole ID, actual detector backend, purpose-specific OpenAI and YOLO
upstream request IDs, and any classified OpenAI exhaustion or YOLO monthly-cap code.
They do not contain coordinates, images, request bodies, signing keys, or a stable
installation identifier.

After each non-OPTIONS response, a best-effort D1 write increments one daily
`route`/`outcome`/`vision_mode` aggregate in `request_metrics_daily`. A request
associated with an installation, including registration, also updates a pseudonymous
per-installation/day count and `last_seen_at` in `installation_activity_daily`;
`/v1/impact` uses those rows for active-installation totals. A metrics-write failure
is logged with the request ID and does not replace the API response. These are
request and participating-installation measures, not unique people or successful
complaints.

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
`prompt_version: "road-damage-v5"`. For a map-capable shared detection, also send
the future report's stable `client_observation_id` and the capture `lat` and `lng`.
Coordinates must be supplied together. `image_detail` defaults to `high`. A pure
YOLO deployment accepts only the universal `high` setting and does not forward the
OpenAI-only detail field to the YOLO gateway. An OpenAI-primary deployment uses the
requested detail; if it falls back to YOLO, the gateway request omits it.

The endpoint returns the flat v4 verdict plus `detector`, per-installation `quota`,
and `request_id`. When the verdict is both `image_quality="acceptable"` and
`assessment="damaged"` and a `client_observation_id` was supplied, it also returns
`detection_receipt` and `detection_receipt_expires_at`. The receipt is bound to the
signed installation, client observation ID, SHA-256 of the decoded image, verdict,
actual backend/model provenance, prompt/schema versions, and supplied coordinates.
A receipt issued without coordinates cannot authorize a public-map report. The
verdict contains exactly these fields:

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
`{lat, lng, address_hint?, lgd_hint?, town_hint?}`. After the ownership and geocoder
gates described below, it limits candidates to the KGIS body's imported tenders (the
five Bengaluru successor corporations also see the legacy `BLR` pool), ranks a
deterministic shortlist, and preferably asks the server OpenAI model to adjudicate
it. The stable matching policy is sent as top-level Responses API instructions. The
reverse-geocoded address and shortlisted database rows are sent separately as a
delimited, untrusted JSON document, with an explicit instruction to use text
inside either source only as evidence and not as model instructions.

The adjudicator must reject contracts whose scope is only a footpath, sidewalk,
pedestrian walkway, kerb, drain, culvert, utility, landscaping, building, park, or
other non-road-surface work, even when the street, locality, or ward matches exactly.
A combined work remains eligible only when its own text explicitly includes roadway
resurfacing, pavement or road repair, pothole filling, rehabilitation, or road
maintenance; a generic phrase such as “improvement work” is not enough.
After model selection, a code-level negative gate also rejects an explicitly different
road named in either the work description or location column. Area-wide, ward-wide,
and unnamed-road packages remain eligible; database text can never instruct this gate.

The response is `{request_id, jurisdiction, tender, reason}`. `tender` is null
when jurisdiction, address, candidates, or confidence is insufficient. A null
result is safer than naming an unrelated contractor. Missing/exhausted server
credit and per-installation daily or project-wide daily/monthly caps use the strict
deterministic location/scope gate; successful fallback responses set
`match_method="deterministic_location_scope"`.
Ordinary rate limits and availability failures remain non-cached HTTP 503 errors
with their existing `shared_*` code and `details.retryable: true`. A genuine
out-of-coverage or no-match result is HTTP 200 and terminal.

Road ownership is a separate server-side prerequisite and is resolved before any
tender rows or model call are used:

1. KGIS Town plus National, State, and District Highway proximity queries must all
   succeed.
2. A highway feature returns HTTP 200 with `tender: null` and reason
   `national_highway`, `state_highway`, or `district_highway`, even if a client
   supplies municipal hints.
3. Otherwise, a Town feature classifies the point as `municipal` and supplies the
   authoritative LGD body key. A Town feature with a blank/missing LGD key fails as
   unknown ownership; `lgd_hint` cannot choose a tender partition.
4. If none of those layers contains the point, the Worker queries KGIS Gram Panchayat. A
   named GP returns terminal reason `rural`; an available empty result returns
   `outside_state`; an unavailable required layer returns retryable HTTP 503
   `road_ownership_unavailable`.
5. Only a municipal result proceeds, and it also requires a successful configured
   reverse-geocoder response. Failure is retryable HTTP 503
   `geolocation_unavailable`. `lgd_hint`, `town_hint`, and `address_hint` do not
   override these ownership and availability checks.

The fixed KGIS calls have no credential setting and no automatic alternative data
source. Operators must monitor their availability. Unknown/incomplete ownership
answers are not put in the five-minute location cache, so an explicit retry performs
fresh lookups. Public-Nominatim admission can also fail closed when its shared one-
request-per-second lease is busy.

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

After a successful authoritative lookup, the endpoint also reconciles the nearest
canonical pothole within the dedupe radius that the signed installation previously
observed: municipal ownership backfills authoritative `lgd`/`town`, while
NH/SH/DH/rural/outside-state ownership clears any stale municipal projection. This
repairs records first uploaded under missing or incomplete ownership data without
accepting arbitrary map edits.

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
  "detection_receipt": "64-lowercase-hex-server-receipt",
  "detector": {
    "provider": "shared_server",
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

With receipt enforcement enabled, a `shared_server` report must carry the receipt
from its corresponding detection. The server validates installation ownership,
expiry, client observation ID, image hash, damage type, size, prompt/schema version,
and location (default tolerance 3 m; `RECEIPT_LOCATION_TOLERANCE_METRES` is clamped
to 1–10 m). The receipt retains the actual backend/model provenance, and the stored
observation takes its model and contract versions from the receipt rather than
trusting report metadata. The receipt is consumed transactionally with the
observation. The same observation and idempotency replay remain safe; the receipt
cannot authorize a different observation.

An observation accepted with a valid receipt is labelled
`server_verified_shared`. Personal-key observations—and legacy receipt-less shared
observations accepted only while the compatibility flag is off—are labelled
`client_attested`. These labels describe whether this server can bind a shared
verdict to the submitted evidence. They do not prove the road condition, genuine-app
provenance, a unique human, or government validation.

Personal-key reports are accepted only for an explicitly supported prompt/schema pair
listed by `supported_personal_detector_contracts` on `/v1/health`. A shared report
with a receipt is checked against the immutable contract stored in that receipt, so a
queued valid observation can survive a later prompt deployment; the caller cannot
invent a different version.

The report endpoint independently performs the KGIS and configured-geocoder lookups;
KGIS supplies authoritative canonical `lgd`/`town` when available. This remains
correct when a native drive report arrives before its tender request. Report upload
does not use the tender endpoint's ownership fail-closed rule: an observation can be
saved during a location-service outage, but its public-map jurisdiction stays empty
unless KGIS verified municipal ownership. Client hints are never persisted as
authoritative map jurisdiction. A later successful signed tender lookup can backfill
authoritative KGIS data on the nearest pothole previously observed by that
installation.

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
  window is 180 days and maximum result count is 2,000. Each feature includes the
  canonical `verification` label plus `verified_shared_observers` and
  `client_attested_observers`. A canonical is labelled `server_verified_shared`
  when at least one distinct observer has a receipt-verified observation; otherwise
  it is `client_attested`.
- `GET /v1/impact?from=YYYY-MM-DD&to=YYYY-MM-DD` returns aggregate requests,
  active installations, new potholes, observations, and distinct observers. Its
  observation totals split `server_verified_shared` from `client_attested` and
  include `verified_distinct_observers`. The default window is the last 30 UTC days.
- `GET /map` is the public Leaflet dashboard backed by those two endpoints.
- `GET /v1/health` exposes configuration and contract versions, but no secret.

The health response retains released-client fields `shared_vision_configured`,
`shared_vision_provider`, and `shared_vision_model`, and adds:

- `shared_vision_provider_mode`, `shared_vision_primary_provider`,
  `shared_vision_primary_configured`, `shared_vision_fallback_provider`,
  `shared_vision_fallback_configured`, `shared_vision_fallback_model`, and
  `supported_shared_vision_providers`;
- `detection_prompt_version` and `detection_schema_version`;
- `supported_personal_detector_contracts`;
- `shared_detection_receipts_required`;
- `operator_geocoder_configured`; and
- `public_nominatim_enabled`.

Those are configuration flags, not readiness checks. In particular, `ok: true`
does not prove provider credit, upstream reachability, KGIS availability, geocoder
credentials, D1/KV write access, receipt-table migration, or imported tender rows.
`public_nominatim_enabled` reflects the opt-in flag; a configured operator endpoint
still takes precedence. The health response has no separate flag for model versus
deterministic tender adjudication.

Public map/impact responses do not expose installation IDs or individual request
history. They intentionally expose canonical pothole coordinates.

## Privacy and retention

The implementation has these concrete storage boundaries; operators should not
promise shorter retention without adding and verifying a deletion policy:

- Image bytes exist in Worker request memory and are sent to the configured detector.
  They are not stored in D1, KV, or structured Worker logs. OpenAI requests set
  `store: false`. The observation and receipt retain the decoded image's SHA-256
  digest, not the image.
- Complete KGIS/geocoder results use a bounded in-isolate cache for at most five
  minutes. KGIS and the selected reverse geocoder receive exact coordinates. A
  successful tender response, including its reverse-geocoded address, and a
  successful detection response are eligible for idempotent replay for 24 hours.
  Request-time expiry enforces that window; daily scheduled cleanup removes the
  expired D1 rows, so physical deletion follows the next successful cleanup run.
- An unused detection receipt expires 30 days after issue. A consumed receipt,
  including its bound coordinates and image digest, is retained for 180 days after
  consumption. Daily scheduled cleanup removes expired unused receipts and consumed
  receipts past that window; physical deletion occurs on a successful scheduled run,
  not at the exact expiry instant.
- KV signed-request replay markers expire after 10 minutes. A model-scoped YOLO cap
  circuit contains only its cap code and reset time and expires at the advertised
  reset. The public-Nominatim D1 gate contains only its next-admission timestamp.
- Installations, public keys, canonical potholes, exact accepted-observation
  coordinates and timestamps, optional GPS/heading/speed metadata, image digests,
  detector metadata, observer relationships, imported tenders, usage counters,
  daily request metrics, and per-installation daily activity have no automatic
  deletion window in this code. Report/activity idempotency records are also not
  covered by the 24-hour detection/tender replay pruning. There is no server-side
  user-account deletion endpoint.
- Structured logs retain the server request ID and operational fields described in
  [Request identity and signing](#request-identity-and-signing), subject to the
  operator's Cloudflare log-retention configuration. Report request IDs are also
  stored with observation records; logs do not include images, coordinates, request
  bodies, signing keys, or stable installation IDs.

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
