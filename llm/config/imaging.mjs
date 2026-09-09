export const imagingConfig = Object.freeze({
  maxDetectionImages: 1,
  manual: Object.freeze({
    maxDimension: 2_000,
    jpegQuality: 0.85,
    roadBand: 1,
    adaptiveBrightness: true,
  }),
  drive: Object.freeze({
    maxDimension: 1_280,
    jpegQuality: 0.85,
    roadBand: 1,
    adaptiveBrightness: true,
  }),
  acceptedEvidence: Object.freeze({
    maxDimension: 4_000,
    jpegQuality: 0.92,
  }),
  adaptiveLuminance: Object.freeze({
    targetSamples: 12_000,
    meanThreshold: 72,
    darkPixelThreshold: 12,
    brightPixelThreshold: 245,
    brightFractionThreshold: 0.08,
    targetMean: 85,
    meanFloor: 35,
    minimumLift: 1.15,
    maximumLift: 1.65,
    contrast: 1.10,
  }),
});
