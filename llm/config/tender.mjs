export const tenderConfig = Object.freeze({
  model: "gpt-5-mini",
  reasoningEffort: "minimal",
  minimumConfidence: 0.60,
  maxCandidates: 25,
  stringLimits: Object.freeze({
    address: 500,
    workDescription: 180,
    divisionOrLocation: 150,
    contractor: 150,
    published: 40,
    reason: 600,
  }),
});
