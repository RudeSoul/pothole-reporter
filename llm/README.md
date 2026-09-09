# Pothole Reporter LLM contract

This folder is the single editable source for every production prompt, structured-output schema, and LLM-specific setting used by the browser, central server, evaluator, and native Android Drive Mode.

## Layout

- `prompts/detection.mjs` — road-damage classification prompt, capture-layout and language variants, eval prompt variants, and response schema.
- `prompts/tender.mjs` — developer-level tender matching instructions, untrusted-data boundary, and response schema.
- `config/models.mjs` — allowed models, the default model, reasoning effort by model, and image-detail capabilities.
- `config/runtime.mjs` — Responses endpoint, retention/verbosity/structured-output switches, streaming policy, and LLM call deadlines.
- `config/imaging.mjs` — image counts and the exact transforms applied before vision requests.
- `config/tender.mjs` — tender model, reasoning effort, threshold, shortlist size, and field bounds.
- `index.mjs` — the canonical combined contract.
- `generate.mjs` — deterministic adapters for JavaScript, Python/evals, browser WebView, and Kotlin.

Detection instructions intentionally remain user-role image-task content to preserve the evaluated production request shape. Tender matching uses top-level developer instructions because its address and database strings are untrusted input; the delimited JSON evidence uses the separately declared `dataRole`. Runtimes consume these role fields from the generated contract rather than repeating role strings.

The current detection contract is `road-damage-v5` / schema version 4. Every request
contains exactly one image and returns exactly five fields:

- `image_quality`: `acceptable` when the image can support a road-damage decision, or
  `rejected` when blur, darkness, obstruction, framing, or distance prevents one.
- `assessment`: `damaged` when visible road-surface damage is present, otherwise
  `undamaged`.
- `damage_type`: `pothole_cavity`, `failed_patch`, `surface_breakup`,
  `rut_or_depression`, `other_road_damage`, or `null` when no usable damage class
  applies.
- `size`: `small`, `medium`, `large`, or `null` when the image cannot support a size.
- `description`: a short factual explanation of the visible evidence or rejection.

Damage may be on asphalt, concrete, gravel, dirt, or mud roads. Damage at the road
edge is still road damage; the model must not reject it merely because it touches a
kerb, gutter, shoulder, drain surround, or road-to-footpath joint. Damage confined to
an intact footpath, kerb, drain, or other non-road object remains `undamaged` for this
road-damage task.

The tender contract explicitly rejects footpath-, sidewalk-, pedestrian-walkway-,
kerb-, drain-, utility-, landscaping-, building-, and park-only works, even when their
street or ward matches exactly. Combined works remain eligible only when their text
also explicitly covers road-surface repair, resurfacing, pavement, pothole filling,
rehabilitation, or maintenance. This prevents locality overlap from turning unrelated
footpath work into a probable road-work tender.

## Editing and verification

Edit only files under `llm/prompts/` or `llm/config/`, then run:

```sh
node llm/generate.mjs
node llm/generate.mjs --check
```

Generated files carry the canonical SHA-256 and must not be edited manually:

- `llm/generated/contract.json` — language-neutral evaluator/test input.
- `llm/generated/contract.mjs` — central-server adapter.
- `static/llm-contract.generated.js` and Android mirrors — browser/WebView adapter.
- `android-app/android/app/src/main/java/com/gauravsen/potholereporter/drivemode/LlmContractGenerated.kt` — native adapter.

`storeResponses` is false so response objects are not retained through the Responses API storage feature. Structured Outputs carry the schemas rather than repeating them inside prompt prose. Detection keeps `minimal` reasoning on `gpt-5-mini` because the project evals found no gain from raising it; the experimental `gpt-5.6` arm remains `none`. Tender matching also uses `minimal`: its labelled shuffled-candidate eval produced the same precision and recall as `medium`, so the cheaper and faster arm is the production default.

Temperature and `top_p` are intentionally omitted: this contract uses reasoning models and strict Structured Outputs, and there is no eval evidence that sampling overrides improve these classifications. `max_output_tokens` is also omitted until an eval establishes a safe ceiling that cannot truncate a valid schema response.

Run the detection benchmark through the commands in [`../eval/README.md`](../eval/README.md).
Run `python3 eval/run_tender_eval.py` for the curated project-policy tender cases. That
harness shuffles candidate order, reports true/false positives and negatives plus
precision, recall, F1, and threshold sweeps, and fails its release gate when either
precision or recall is below the recorded target. An all-null matcher therefore cannot
pass merely by avoiding false positives. This regression fixture is not a sealed,
population-representative human-labelled accuracy set.
