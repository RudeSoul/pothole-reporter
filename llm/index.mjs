import { detectionPrompt } from "./prompts/detection.mjs";
import { tenderPrompt } from "./prompts/tender.mjs";
import { modelConfig } from "./config/models.mjs";
import { runtimeConfig } from "./config/runtime.mjs";
import { imagingConfig } from "./config/imaging.mjs";
import { tenderConfig } from "./config/tender.mjs";

export {
  detectionPrompt,
  tenderPrompt,
  modelConfig,
  runtimeConfig,
  imagingConfig,
  tenderConfig,
};

export const llmContract = Object.freeze({
  contractVersion: 2,
  prompts: Object.freeze({
    detection: detectionPrompt,
    tender: tenderPrompt,
  }),
  config: Object.freeze({
    models: modelConfig,
    runtime: runtimeConfig,
    imaging: imagingConfig,
    tender: tenderConfig,
  }),
});
