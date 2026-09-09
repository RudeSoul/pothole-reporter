export const runtimeConfig = Object.freeze({
  responsesUrl: "https://api.openai.com/v1/responses",
  modelsUrl: "https://api.openai.com/v1/models?limit=1",
  storeResponses: false,
  textVerbosity: "low",
  strictStructuredOutputs: true,
  personalDetectionStream: true,
  timeoutsMs: Object.freeze({
    personalOpenAI: 30_000,
    sharedVisionClient: 100_000,
    serverOpenAIMax: 55_000,
    serverYoloMax: 30_000,
    serverUpstreamMin: 5_000,
  }),
});
