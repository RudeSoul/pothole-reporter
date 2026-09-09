# Road-damage evaluation

`run_eval.py` replays the production `road-damage-v5` contract against labelled
manual photos or Drive Mode frames. The contract returns exactly five fields:
`image_quality`, `assessment`, `damage_type`, `size`, and `description`.

```bash
node llm/generate.mjs --check
python3 eval/run_eval.py --mode manual --trials 5
python3 eval/run_eval.py --mode drive --trials 5
python3 eval/run_eval.py --mode drive --models gpt-5-mini --details high,original
```

`OPENAI_API_KEY` is read from the environment or the repository-root `.env`.
Use `--dry-run` to validate the selected image transform and request configuration
without calling the API.

## Production parity

Every request contains exactly one image. Manual replay uses
`IMAGING_CONFIG.manual`; Drive Mode uses `IMAGING_CONFIG.drive`. If a labelled
event contains `frames`, `primary_index` identifies the one selected frame. The
harness does not send the surrounding sequence to the model.

The evaluator reads its prompt, schema, model allow-list, image-detail rules,
reasoning effort, role, strict-output setting, and image transforms from
`llm/generated/contract.json`. It mirrors the production resize and sampled-luma
brightness adjustment. Pillow and Android/WebView codecs are not byte-identical,
so the recorded JPEG hashes identify evaluator inputs; they do not claim identical
encoded bytes on every client.

The request always sends `store: false`. The harness refuses to run if the
canonical contract enables response storage.

Decision policy:

- `image_quality: rejected` becomes `review`, never an accepted complaint.
- `acceptable` + `damaged` + a non-null recognised `damage_type` becomes `accept`.
- `acceptable` + `undamaged` + null `damage_type` and `size` becomes `reject`.
- Contradictory or incomplete combinations become `review`.

## Metrics and repetitions

Repeated calls measure stochastic variation; they are not extra ground-truth
examples. Rows are grouped by `event_id` (or image path when no event ID exists),
and each event contributes exactly once to TP, FP, TN, or FN. An event is predicted
positive only when more than half its repetitions return `accept`; ties fail closed.

The summary reports event-grouped precision, recall, specificity, false-accept
rate (FAR), and F1 in two sections:

- `owner_verified` contains only labels whose `labelled_by` value is exactly
  `owner`.
- `provisional_including_unverified` also includes assistant-labelled examples and
  must not be presented as verified accuracy.

The current seed has owner-verified positives but no owner-verified negatives.
Verified recall can therefore be measured, but verified precision, specificity,
FAR, and F1 are intentionally reported as `null`/`n/a`. Do not claim verified
precision until a sealed set contains owner-labelled ordinary-road negatives.

## Images and labels

Images are not committed. Put the seed images under `eval/images/seed/` and keep
their provenance and licence in `eval/labels.json`. Supported positive labels are
`pothole_cavity`, `failed_patch`, `surface_breakup`, `rut_or_depression`,
`other_road_damage`, plus legacy `pothole`; `damaged` is also accepted as a generic
positive label. Supported negative labels are `not_pothole` and `undamaged`.
Disputed or unlabelled items run but are excluded from binary metrics.

Set `mode` to `manual` or `drive` on new entries. For legacy entries only, a source
containing `dashcam` is inferred as Drive Mode. Keep multiple frames from one road
encounter under one `event_id` so train/test splits and metrics do not leak adjacent
views across events.

The bundled seed is diagnostic, not a release gate: it lacks verified negatives,
true captured Drive Mode sequences, and enough independent locations. A release
set must be sealed, human-labelled, grouped by encounter, and include ordinary
negative roads as well as each supported damage type.

## Tender matching regression

`run_tender_eval.py` calls the production shortlist implementation and the canonical
tender prompt/schema against `tender_cases.json`. Cases cover exact road work,
footpath-only, drain-only, pedestrian-only, combined road-and-drain, ward-wide
pothole maintenance, locality mismatch, and instruction-like database text.

```bash
python3 eval/run_tender_eval.py --trials 3
python3 eval/run_tender_eval.py --reasoning minimal --trials 3
```

Candidate order is shuffled per trial. The summary separately reports shortlist
recall and model exact-selection TP/FP/TN/FN, precision, recall, specificity, F1,
wrong-contract selections, and a confidence-threshold sweep. The command fails when
precision or recall is below its configured gate, so an all-null matcher fails recall.
The fixture is curated from explicit project policy and selected real contracts; it is
not a sealed or population-representative human-labelled accuracy set.

## Prompt arms and outputs

`baseline` is the shipped detection prompt. Additional arms are available only
when registered in `evaluationVariants` in `llm/prompts/detection.mjs`; ad-hoc
prompt files are not loaded.

Each run writes:

- `raw.jsonl`: trial-level model outputs and decisions
- `summary.json`: event-grouped verified and provisional metrics
- `manifest.json`: contract hashes, configuration, and label-set hash
- `cache/`: one stable response slot per event, configuration, and repetition
