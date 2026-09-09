package com.gauravsen.potholereporter.drivemode

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class DetectionDispatcherPolicyTest {
    @Test
    fun miniKeepsMinimalReasoningWhileGpt56UsesNone() {
        assertEquals(
            LlmContractGenerated.DEFAULT_REASONING_EFFORT,
            reasoningEffortForModel(LlmContractGenerated.DEFAULT_MODEL),
        )
        val experimentalModel = LlmContractGenerated.ORIGINAL_DETAIL_MODELS.single()
        assertEquals(
            LlmContractGenerated.REASONING_EFFORT_BY_MODEL.getValue(experimentalModel),
            reasoningEffortForModel(experimentalModel),
        )
    }

    @Test
    fun visionLanguageAllowsKannadaAndDefaultsEverythingElseToEnglish() {
        val translatedLanguage = LlmContractGenerated.ALLOWED_LANGUAGES
            .single { it != LlmContractGenerated.DEFAULT_LANGUAGE }
        assertEquals(translatedLanguage, normalizeVisionLanguage(translatedLanguage))
        assertEquals(
            LlmContractGenerated.DEFAULT_LANGUAGE,
            normalizeVisionLanguage(LlmContractGenerated.DEFAULT_LANGUAGE),
        )
        assertEquals(LlmContractGenerated.DEFAULT_LANGUAGE, normalizeVisionLanguage(null))
        assertEquals(
            LlmContractGenerated.DEFAULT_LANGUAGE,
            normalizeVisionLanguage("unsupported"),
        )
    }

    @Test
    fun sharedDetectionForwardsTheSelectedImageDetail() {
        val originalDetail = LlmContractGenerated.ALLOWED_IMAGE_DETAILS
            .single { it != LlmContractGenerated.DEFAULT_IMAGE_DETAIL }
        val detection = sharedVisionRequestConfig(
            language = LlmContractGenerated.ALLOWED_LANGUAGES
                .single { it != LlmContractGenerated.DEFAULT_LANGUAGE },
            model = LlmContractGenerated.ORIGINAL_DETAIL_MODELS.single(),
            detail = originalDetail,
            promptVersion = LlmContractGenerated.DETECT_PROMPT_VERSION,
        )
        assertEquals(originalDetail, detection["image_detail"])
        assertEquals(LlmContractGenerated.DETECT_PROMPT_VERSION, detection["prompt_version"])
    }

    @Test
    fun nativePromptsAreBuiltFromTheGeneratedContract() {
        val translatedLanguage = LlmContractGenerated.ALLOWED_LANGUAGES
            .single { it != LlmContractGenerated.DEFAULT_LANGUAGE }
        val detection = detectionPromptForDrive(translatedLanguage)
        assertTrue(detection.startsWith(LlmContractGenerated.DETECT_PROMPT))
        assertTrue(detection.contains(LlmContractGenerated.DETECT_CAPTURE_DRIVE))
        assertTrue(detection.endsWith(LlmContractGenerated.DETECT_LANGUAGE_KN))
    }

    @Test
    fun nativeBoundaryNormalizesModelAndImageDetailFromGeneratedPolicy() {
        val originalModel = LlmContractGenerated.ORIGINAL_DETAIL_MODELS.single()
        val originalDetail = LlmContractGenerated.ALLOWED_IMAGE_DETAILS
            .single { it != LlmContractGenerated.DEFAULT_IMAGE_DETAIL }
        assertEquals(LlmContractGenerated.DEFAULT_MODEL, normalizeVisionModel("unsupported"))
        assertEquals(
            LlmContractGenerated.DEFAULT_IMAGE_DETAIL,
            normalizeVisionDetail(originalDetail, LlmContractGenerated.DEFAULT_MODEL),
        )
        assertEquals(originalDetail, normalizeVisionDetail(originalDetail, originalModel))
    }

    @Test
    fun binaryDetectionDecisionRejectsContradictoryDamageDetails() {
        assertEquals(
            "accept",
            detectionDecisionFor("acceptable", "damaged", "pothole_cavity", "medium"),
        )
        assertEquals(
            "reject",
            detectionDecisionFor("acceptable", "undamaged", null, null),
        )
        assertEquals(
            "review",
            detectionDecisionFor("rejected", "undamaged", null, null),
        )
        assertEquals(
            "review",
            detectionDecisionFor("acceptable", "damaged", null, null),
        )
        assertEquals(
            "review",
            detectionDecisionFor("acceptable", "damaged", "cat", "medium"),
        )
        assertEquals(
            "review",
            detectionDecisionFor("acceptable", "damaged", "pothole_cavity", "huge"),
        )
        assertEquals(
            "review",
            detectionDecisionFor("acceptable", "undamaged", "surface_breakup", null),
        )
        assertEquals(
            "review",
            detectionDecisionFor("acceptable", "undamaged", null, "small"),
        )
    }
}
