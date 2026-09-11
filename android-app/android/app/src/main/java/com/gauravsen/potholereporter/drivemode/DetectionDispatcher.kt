package com.gauravsen.potholereporter.drivemode

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.IOException
import java.io.InputStreamReader
import java.util.concurrent.TimeUnit

data class DetectionResult(
    val assessment: String,
    val imageQuality: String,
    val damageType: String?,
    val size: String?,
    val description: String,
    val decision: String,
    val detectionReceipt: String? = null,
)

internal fun normalizeVisionLanguage(value: String?): String =
    value?.takeIf { it in LlmContractGenerated.ALLOWED_LANGUAGES }
        ?: LlmContractGenerated.DEFAULT_LANGUAGE

internal fun normalizeVisionModel(value: String?): String =
    value?.takeIf { it in LlmContractGenerated.ALLOWED_MODELS }
        ?: LlmContractGenerated.DEFAULT_MODEL

internal fun normalizeVisionDetail(value: String?, model: String): String {
    val picked = value?.takeIf { it in LlmContractGenerated.ALLOWED_IMAGE_DETAILS }
        ?: LlmContractGenerated.DEFAULT_IMAGE_DETAIL
    return if (picked == LlmContractGenerated.ORIGINAL_IMAGE_DETAIL
        && normalizeVisionModel(model) !in LlmContractGenerated.ORIGINAL_DETAIL_MODELS) {
        LlmContractGenerated.DEFAULT_IMAGE_DETAIL
    } else {
        picked
    }
}

internal fun reasoningEffortForModel(model: String): String =
    LlmContractGenerated.REASONING_EFFORT_BY_MODEL[normalizeVisionModel(model)]
        ?: LlmContractGenerated.DEFAULT_REASONING_EFFORT

/**
 * Resolve the selected provider at the last native boundary before inference.
 *
 * A missing personal key is not a configuration error: it selects the shared service.
 * This keeps an upgraded WebView preference from stranding background Drive Mode in a
 * repeated "key required" failure. A real key still honours an explicit Personal choice,
 * and an explicit Shared choice never leaks that key to the project service.
 */
internal fun effectiveVisionProvider(provider: String?, apiKey: String): String {
    val requested = when (provider?.trim()) {
        null, "" -> if (apiKey.isBlank()) "shared_server" else "personal"
        "shared", "shared_server" -> "shared_server"
        "personal", "personal_openai", "own_key" -> "personal"
        else -> throw IllegalArgumentException("Unknown vision provider")
    }
    return if (requested == "personal" && apiKey.isBlank()) "shared_server" else requested
}

internal fun detectionPromptForDrive(language: String): String =
    LlmContractGenerated.DETECT_PROMPT +
        LlmContractGenerated.DETECT_CAPTURE_DRIVE +
        if (normalizeVisionLanguage(language) == LlmContractGenerated.DEFAULT_LANGUAGE) {
            ""
        } else {
            LlmContractGenerated.DETECT_LANGUAGE_KN
        }

internal fun sharedVisionRequestConfig(
    language: String,
    model: String,
    detail: String,
    promptVersion: String,
): Map<String, String> = mapOf(
    "language" to normalizeVisionLanguage(language),
    "model" to model,
    "image_detail" to detail,
    "prompt_version" to promptVersion,
)

internal fun driveCaptureProvenanceFields(): Map<String, String> = mapOf(
    "capture_source" to "drive_live",
    "location_source" to "device_gps",
)

internal fun sharedVisionObservationFields(
    clientObservationId: String,
    lat: Double,
    lng: Double,
): Map<String, Any> {
    require(clientObservationId.isNotBlank()) { "Client observation ID is required" }
    require(lat.isFinite() && lat in -90.0..90.0) { "Invalid observation latitude" }
    require(lng.isFinite() && lng in -180.0..180.0) { "Invalid observation longitude" }
    return mapOf(
        "client_observation_id" to clientObservationId,
        "lat" to lat,
        "lng" to lng,
    ) + driveCaptureProvenanceFields()
}

internal fun sharedVisionIdempotencyKey(clientObservationId: String): String =
    "vision-${CentralServiceIdentity.sha256Hex(
        clientObservationId.toByteArray(Charsets.UTF_8),
    ).take(40)}"

internal fun normalizeDetectionReceipt(value: String?): String? = value
    ?.trim()
    ?.lowercase()
    ?.takeIf { it.matches(Regex("^[a-f0-9]{64}$")) }

internal fun detectionDecisionFor(
    imageQuality: String,
    assessment: String,
    damageType: String?,
    size: String?,
): String = when {
    imageQuality == "rejected" -> "review"
    imageQuality !in LlmContractGenerated.DETECT_IMAGE_QUALITIES -> "review"
    imageQuality != "acceptable" -> "review"
    assessment == "damaged" && damageType in LlmContractGenerated.DETECT_DAMAGE_TYPES &&
        (size == null || size in LlmContractGenerated.DETECT_SIZES) -> "accept"
    assessment == "undamaged" && damageType == null && size == null -> "reject"
    else -> "review"
}

class DetectionDispatcher(
    provider: String,
    private val apiKey: String,
    private val serviceUrl: String,
    private val serviceIdentity: CentralServiceIdentity,
    model: String = LlmContractGenerated.DEFAULT_MODEL,
    detail: String = LlmContractGenerated.DEFAULT_IMAGE_DETAIL,
    language: String = LlmContractGenerated.DEFAULT_LANGUAGE,
) {
    private val provider = effectiveVisionProvider(provider, apiKey)
    private val model = normalizeVisionModel(model)
    private val detail = normalizeVisionDetail(detail, this.model)
    private val language = normalizeVisionLanguage(language)

    companion object {
        const val MAX_IN_FLIGHT = 6
    }

    private val semaphore = Semaphore(MAX_IN_FLIGHT)
    private val client = OkHttpClient.Builder()
        .connectTimeout(LlmContractGenerated.PERSONAL_OPENAI_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .readTimeout(LlmContractGenerated.PERSONAL_OPENAI_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .writeTimeout(LlmContractGenerated.PERSONAL_OPENAI_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .build()
    private val sharedClient = client.newBuilder()
        .readTimeout(LlmContractGenerated.SHARED_VISION_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .callTimeout(LlmContractGenerated.SHARED_VISION_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .build()

    /** Analyze exactly one selected Drive Mode frame. */
    suspend fun detect(
        imageBase64: String,
        clientObservationId: String,
        lat: Double,
        lng: Double,
    ): DetectionResult = withContext(Dispatchers.IO) {
        semaphore.withPermit {
            if (provider == "shared_server") {
                return@withPermit detectViaService(imageBase64, clientObservationId, lat, lng)
            }
            if (apiKey.isBlank()) {
                throw IllegalStateException("OpenAI API key is required in personal-key mode")
            }

            val content = JSONArray()
                .put(JSONObject()
                    .put("type", "input_image")
                    .put("image_url", jpegDataUrl(imageBase64))
                    .put("detail", detail))
                .put(JSONObject()
                    .put("type", "input_text")
                    .put("text", detectionPromptForDrive(language)))
            val input = JSONObject()
                .put("role", LlmContractGenerated.DETECT_PROMPT_ROLE)
                .put("content", content)
            val format = JSONObject()
                .put("type", "json_schema")
                .put("name", LlmContractGenerated.DETECT_SCHEMA_NAME)
                .put("schema", JSONObject(LlmContractGenerated.DETECT_SCHEMA))
                .put("strict", LlmContractGenerated.STRICT_STRUCTURED_OUTPUTS)
            val requestBody = JSONObject()
                .put("model", model)
                .put("input", JSONArray().put(input))
                .put("text", JSONObject()
                    .put("format", format)
                    .put("verbosity", LlmContractGenerated.TEXT_VERBOSITY))
                .put("store", LlmContractGenerated.STORE_RESPONSES)
                .put("reasoning", JSONObject()
                    .put("effort", reasoningEffortForModel(model)))
                .put("stream", LlmContractGenerated.PERSONAL_DETECTION_STREAM)

            val request = Request.Builder()
                .url(LlmContractGenerated.RESPONSES_URL)
                .addHeader("Authorization", "Bearer $apiKey")
                .post(requestBody.toString()
                    .toRequestBody("application/json; charset=utf-8".toMediaType()))
                .build()

            val accumulatedText = StringBuilder()
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    throw IOException("OpenAI detection failed (${response.code})")
                }
                val body = response.body ?: throw IOException("OpenAI returned no response body")
                val reader = BufferedReader(InputStreamReader(body.byteStream()))
                var line: String?
                while (reader.readLine().also { line = it } != null) {
                    if (!line!!.startsWith("data: ")) continue
                    val eventText = line!!.substring(6).trim()
                    if (eventText == "[DONE]") break
                    val event = runCatching { JSONObject(eventText) }.getOrNull() ?: continue
                    val delta = when {
                        event.optString("type") == "response.output_text.delta" ->
                            event.optString("delta").takeIf { it.isNotEmpty() }
                        event.optJSONObject("output_text")?.has("delta") == true ->
                            event.optJSONObject("output_text")?.optString("delta")
                        else -> null
                    }
                    if (delta != null) accumulatedText.append(delta)
                }
            }
            if (accumulatedText.isEmpty()) {
                throw IOException("OpenAI returned an empty detection response")
            }
            val json = runCatching { JSONObject(accumulatedText.toString()) }
                .getOrElse { throw IOException("OpenAI returned invalid detection data", it) }
            resultFromJson(json)
        }
    }

    /** Shared mode sends image evidence and configuration; the server owns the prompt. */
    private fun detectViaService(
        imageBase64: String,
        clientObservationId: String,
        lat: Double,
        lng: Double,
    ): DetectionResult {
        if (serviceUrl.isBlank()) throw IllegalStateException("Central service URL is missing")
        val path = "/v1/vision/detect"
        val body = JSONObject()
            .put("images", JSONArray().put(JSONObject()
                .put("data_url", jpegDataUrl(imageBase64))))
            .put("capture_mode", "drive")
        sharedVisionRequestConfig(
            language,
            model,
            detail,
            LlmContractGenerated.DETECT_PROMPT_VERSION,
        ).forEach { (key, value) -> body.put(key, value) }
        sharedVisionObservationFields(clientObservationId, lat, lng)
            .forEach { (key, value) -> body.put(key, value) }
        val result = resultFromJson(executeShared(
            path,
            body,
            sharedVisionIdempotencyKey(clientObservationId),
        ))
        if (result.decision == "accept" && result.detectionReceipt == null) {
            throw IOException("Shared vision returned no detection receipt")
        }
        return result
    }

    private fun executeShared(path: String, bodyObject: JSONObject, idempotency: String): JSONObject {
        val body = bodyObject.toString().toByteArray(Charsets.UTF_8)
        val signed = serviceIdentity.sign(serviceUrl, "POST", path, body, idempotency)
        val request = Request.Builder()
            .url("${serviceUrl.trimEnd('/')}$path")
            .addHeader("X-Install-ID", signed.installId)
            .addHeader("X-Timestamp", signed.timestamp)
            .addHeader("X-Signature", signed.signature)
            .addHeader("Idempotency-Key", signed.idempotencyKey)
            .post(body.toRequestBody("application/json; charset=utf-8".toMediaType()))
            .build()

        sharedClient.newCall(request).execute().use { response ->
            val requestId = response.header("X-Request-ID")
            val text = response.body?.string().orEmpty()
            val json = runCatching { JSONObject(text) }.getOrElse {
                throw IOException(
                    "Shared vision returned invalid data${requestId?.let { " [request $it]" } ?: ""}",
                    it,
                )
            }
            if (!response.isSuccessful) {
                val message = json.optString("message")
                    .ifBlank { "Shared vision is unavailable (${response.code})" }
                throw IOException("$message${requestId?.let { " [request $it]" } ?: ""}")
            }
            android.util.Log.i("DetectionDispatcher", "Shared vision request_id=$requestId")
            return json
        }
    }

    private fun jpegDataUrl(value: String): String =
        if (value.startsWith("data:")) value else "data:image/jpeg;base64,$value"

    private fun resultFromJson(json: JSONObject): DetectionResult {
        if (LlmContractGenerated.DETECT_REQUIRED_FIELDS.any { !json.has(it) }) {
            throw IOException("Detection response is missing required fields")
        }
        val imageQuality = json.getString("image_quality")
        val assessment = json.getString("assessment")
        val damageType = if (json.isNull("damage_type")) null else json.getString("damage_type")
        val size = if (json.isNull("size")) null else json.getString("size")
        val detectionReceipt = if (!json.has("detection_receipt")
            || json.isNull("detection_receipt")) null
        else normalizeDetectionReceipt(json.optString("detection_receipt"))
        return DetectionResult(
            assessment = assessment,
            imageQuality = imageQuality,
            damageType = damageType,
            size = size,
            description = json.getString("description"),
            decision = detectionDecisionFor(imageQuality, assessment, damageType, size),
            detectionReceipt = detectionReceipt,
        )
    }
}
