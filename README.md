# Pothole Reporter

An Android app that detects road damage, associates the location with a supported
urban local body, and drafts an email complaint. A privacy-minimised central service
now resolves tender details, groups nearby sightings into canonical potholes, and
publishes aggregate impact metrics and a general map.
There is no user account system.

**Current coverage:** supported Karnataka urban local bodies with published contact
emails. National, state and district highways, and rural roads are excluded.

<a href="docs/example-pothole.jpg"><img src="docs/example-pothole-thumb.jpg" width="280" alt="Pothole detected by Pothole Reporter"></a>

<sub>Example detected by the app. Select the thumbnail for the full-size photo.</sub>

## What it does

- Captures exactly one photo per analysis request, either manually or by sampling the
  road in Drive Mode.
- Uses either the shared detector selected by the project (OpenAI by default, with an
  in-house YOLO gateway supported) or a person's own OpenAI key to identify pothole
  cavities, failed patches, surface breakup, and ruts or depressions.
- Lets people use the project's best-effort shared vision allowance or their own
  OpenAI API key. A personal key is sent directly to OpenAI and never to the project
  service.
- Adds the location, matching municipal boundary, published officer address, and a
  probable road-work contract returned by the central service when a reliable match
  is available.
- Sends accepted sighting metadata to the central service, deduplicates nearby reports,
  and shows canonical road-damage locations on a shared map.
- Provides one complaint action: **Email complaint**. One tap opens a pre-addressed,
  editable email draft with the road photo, location, damage details, and—only when
  matched—the probable tender number.
- Offers optional local Drive Mode recording, off by default. Saved footage can be
  analysed or deleted later from History.
- Imports one or several road-video clips from a phone gallery or file picker. On
  Android, videos can also be shared directly to Pothole Reporter from Photos, Files,
  or a dashcam companion app. A Meta-glasses clip is first transferred to the phone
  with Meta AI, then selected from the gallery. The original video remains local; the
  app sends only sampled JPEG frames for detection.
- Groups repeat Drive/footage observations into one event; Debug mode keeps every
  accepted observation.

The app refuses to guess a recipient for national, state and district highways, rural
roads, locations outside Karnataka, unknown road ownership, or bodies without a
published address.

## Install and use

1. Download `PotholeReporter.apk` from the
   [latest release](https://github.com/coding-parrot/pothole-reporter/releases/latest)
   and sideload it.
2. Choose shared vision or enter your own OpenAI API key, then allow camera and
   location access.
3. For Drive Mode, mount the phone securely and keep the road inside the orange guide.
   Start the drive before moving and do not interact with the phone while driving.
4. Alternatively, use **Report road damage** while safely stopped, or choose
   **Import Meta glasses / dashcam video**. Select segmented dashcam clips in recording
   order. Add a timestamped GPX track when available so detections can be routed.
5. Review the detected damage, location, recipient, and any probable contract match,
   then tap **Email complaint**. Review or edit the draft and press Send in your email app.

Browser Drive Mode requires the page to remain in the foreground. On Android, the
optional no-preview background mode uses a visible foreground-service notification and
continues until you stop it. An internet connection is required for detection,
geocoding, and road classification.

## Important limits

- Detection is not perfect. It can miss damage or produce false positives; review every
  result before sending it.
- Detection defaults to `gpt-5-mini` with high image detail. The `gpt-5.6`/original-detail
  option is experimental; neither has a complete held-out, human-labelled v4 field
  benchmark. See [`eval/README.md`](eval/README.md).
- Contract matches are probable matches, not proof of responsibility or warranty.
  Footpath-, drain-, utility-, and other non-road-only works are excluded even when
  their locality matches; a combined work must explicitly include road-surface work.
- Imported footage needs trustworthy location data for authority, contractor, tender,
  map, and email routing. The app never silently labels an old video with the phone's
  current position. Use a timestamped GPX track, or explicitly opt into the current
  position only when every clip was recorded at that location. Detection can continue
  without location, but those results remain unrouted.
- Video decoding is device- and codec-dependent. H.264/AVC in MP4 is the safest
  dashcam interchange format; unsupported files fail visibly instead of being counted
  as an analysed video.
- The app does not send email automatically and is not affiliated with any government
  body.

## Cost and privacy

- Shared vision is centrally sponsored and best effort. It can fail when the configured
  detector is unavailable or at capacity, or when the default OpenAI backend is out of
  credits. Personal-key usage is billed to that OpenAI key; Drive Mode can make many
  image requests.
- Drive recording uses roughly 18 MB per minute while enabled. Successful reanalysis
  deletes it unless Debug mode is keeping it.
- Imported videos are sampled on the device under an explicit per-run request budget;
  the complete video and its audio are never uploaded. A clip received through another
  Android app's Share action may be copied temporarily into app-private cache so the
  sender cannot revoke access midway through analysis; it is bounded and removed after
  use, discard, or automatic expiry.
- In shared mode, checked images pass through the project service without being retained
  there and go to its configured detector (OpenAI by default or an in-house YOLO
  gateway). In personal-key mode they go directly to OpenAI. Faces, number plates, and
  shopfronts are not blurred.
- The central service stores a pseudonymous installation identifier, accepted pothole
  coordinates, detection metadata, an image hash (not the image), deduplicated
  observations, and aggregate metrics. Request counts are retained as daily aggregates;
  request IDs appear in operational logs.
- Active installations are an impact proxy, not a count of unique people.
- Reports, photos, labels, and footage remain stored locally. Complaint content and
  attachments are handed to your email app only when you open a draft.
- See the [privacy policy](https://coding-parrot.github.io/pothole-reporter/privacy.html)
  and [data sources and limits](https://coding-parrot.github.io/pothole-reporter/sources.html).

## Development

The maintained source files are:

- `llm/` — the single editable prompt, schema, model, reasoning, image-detail,
  timeout, and vision-input configuration package; generated adapters keep every
  runtime aligned
- `static/index.html` — interface and capture workflow
- `static/standalone.js` — detection, storage, routing, and drafting engine
- `server/` — Cloudflare Worker, D1 schema, tender importer, map, and API tests
- `ml/yolo/` — fail-closed dataset preparation, fine-tuning, held-out evaluation,
  and raw ONNX release pipeline
- `infra/aws-yolo/` — authenticated AWS Lambda YOLO gateway with transactional
  monthly request and estimated-compute caps

The long-term phone, dashcam, Meta-glasses, YOLO, and email-routing design is in
[`docs/SCALABLE-INTEGRATIONS.md`](docs/SCALABLE-INTEGRATIONS.md). The boundary that
keeps government complaint filing email-only is in
[`docs/GBA_INTEGRATION.md`](docs/GBA_INTEGRATION.md).

Build the Android APK with:

```bash
./tools/build-apk.sh
```

The script mirrors the static files into the Android project, builds the APK, verifies
the packaged assets, and scans the packaged engine for an OpenAI-style key.

Run the complete test suite with:

```bash
./tests/run-all.sh
```

Run and deploy the central service separately using the commands and required secrets
in [`server/README.md`](server/README.md). The checked-in Wrangler file contains
deployment-specific placeholder IDs; the service is not deployable until those are
replaced and a shared detector is configured. Tender resolution uses model adjudication
when the server has `OPENAI_API_KEY`; without it, the server uses its conservative
deterministic location-and-scope matcher. Shared image detection still requires either
the server OpenAI provider or a deployed/configured YOLO provider.

The server supports the requested `openai_then_http_yolo` chain, but it remains
disabled until an evaluated model and AWS endpoint exist. No model binary is checked
in: the accessible owned labels do not yet contain detection boxes and the referenced
public dataset's broad road-damage class is not pothole-only. The training pipeline
refuses to fabricate either. Complete the human box audits described in
[`ml/yolo/README.md`](ml/yolo/README.md), pass the sealed validation/test gates, then
follow [`infra/aws-yolo/README.md`](infra/aws-yolo/README.md) to deploy within explicit
caps and enable the chain.

These commands assume the Node, Android/JDK, Python, Playwright, and Gradle dependencies
are already installed. The suite requires `OPENAI_API_KEY` in `.env`; some tests call
live OpenAI and Karnataka GIS services. Data-source notes are in
[`docs/SOURCES.md`](docs/SOURCES.md).

## License

Application source in this repository is MIT; see [LICENSE](LICENSE). Ultralytics
training software, base checkpoints, and resulting fine-tuned models have separate
AGPL-3.0/Enterprise terms. Resolve that choice before deploying a private or
proprietary YOLO service; details are in [`ml/yolo/README.md`](ml/yolo/README.md).
