package com.gauravsen.potholereporter.drivemode

import android.util.Base64
import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

internal fun normalizeCentralAddressHint(value: String?): String? =
    value?.trim()?.takeIf { it.isNotEmpty() }

/** Small signed client for civic data that is central regardless of vision provider. */
class CentralServiceException(
    val status: Int,
    val code: String,
    message: String,
    val details: JSONObject? = null,
) : IOException(message) {
    val retryable: Boolean
        get() = status == 408 || status == 425 || status == 429 || status >= 500
}

class CentralServiceClient(
    serviceUrl: String,
    private val identity: CentralServiceIdentity,
) {
    data class PotholeSync(
        val id: Long,
        val duplicate: Boolean,
        val seenCount: Int,
        val requestId: String?,
    )

    data class TenderResolution(
        val address: String?,
        val bodyLgd: String?,
        val bodyName: String?,
        val roadOwnership: String,
        val ownershipDetail: String?,
        val tenderNumber: String?,
        val contractor: String?,
        val tenderNote: String?,
        val reason: String?,
        val requestId: String?,
    )

    companion object {
        private const val TAG = "CentralServiceClient"
        private const val TIMEOUT_MS = 30_000L

        private fun nullableString(json: JSONObject?, key: String): String? {
            if (json == null || !json.has(key) || json.isNull(key)) return null
            return json.optString(key).takeIf { it.isNotBlank() && it != "null" }
        }
    }

    private val base = CentralServiceIdentity.normalizeServiceUrl(serviceUrl)
    private val client = OkHttpClient.Builder()
        .connectTimeout(TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .readTimeout(TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .writeTimeout(TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .build()

    private fun post(
        path: String,
        json: JSONObject,
        idempotencyKey: String = "",
        retryUnknownInstallation: Boolean = true,
    ): Pair<JSONObject, String?> {
        val body = json.toString().toByteArray(Charsets.UTF_8)
        val signed = identity.sign(base, "POST", path, body, idempotencyKey)
        val request = Request.Builder()
            .url("$base$path")
            .addHeader("X-Install-ID", signed.installId)
            .addHeader("X-Timestamp", signed.timestamp)
            .addHeader("X-Signature", signed.signature)
            .apply {
                if (signed.idempotencyKey.isNotEmpty()) {
                    addHeader("Idempotency-Key", signed.idempotencyKey)
                }
            }
            .post(body.toRequestBody("application/json; charset=utf-8".toMediaType()))
            .build()
        client.newCall(request).execute().use { response ->
            val requestId = response.header("X-Request-ID")
            val text = response.body?.string().orEmpty()
            val parsed = runCatching { JSONObject(text) }.getOrElse { JSONObject() }
            if (!response.isSuccessful) {
                val code = parsed.optString("error").ifBlank { "http_${response.code}" }
                val message = parsed.optString("message")
                    .ifBlank { "Central service request failed (${response.code})" }
                if (code == "unknown_installation" && retryUnknownInstallation) {
                    identity.forgetRegistration(base)
                    return post(path, json, idempotencyKey, retryUnknownInstallation = false)
                }
                throw CentralServiceException(
                    response.code,
                    code,
                    "$message${requestId?.let { " [request $it]" } ?: ""}",
                    parsed.optJSONObject("details"),
                )
            }
            return parsed to requestId
        }
    }

    fun reportPothole(
        clientObservationId: String,
        observedAtMs: Long,
        lat: Double,
        lng: Double,
        gpsAccuracy: Float?,
        heading: Float?,
        speed: Float?,
        damageType: String,
        size: String?,
        imageHash: String,
        detectionReceipt: String?,
        detectorProvider: String,
        model: String,
        detail: String,
        evidenceCount: Int,
    ): PotholeSync {
        val detector = JSONObject()
            .put("provider", detectorProvider)
            .put("model", model)
            .put("detail", detail)
            .put("prompt_version", LlmContractGenerated.DETECT_PROMPT_VERSION)
            .put("schema_version", LlmContractGenerated.DETECT_SCHEMA_VERSION)
            .put("evidence_count", evidenceCount)
        val body = JSONObject()
            .put("client_observation_id", clientObservationId)
            .put("observed_at", observedAtMs)
            .put("lat", lat)
            .put("lng", lng)
            .put("gps_accuracy_m", gpsAccuracy ?: JSONObject.NULL)
            .put("heading_deg", heading ?: JSONObject.NULL)
            .put("speed_mps", speed ?: JSONObject.NULL)
            .put("damage_type", damageType)
            .put("size", size ?: JSONObject.NULL)
            .put("image_hash", imageHash)
            .put("detector", detector)
        if (!detectionReceipt.isNullOrBlank()) {
            body.put("detection_receipt", detectionReceipt)
        }
        val idempotency = "drive-${CentralServiceIdentity.sha256Hex(clientObservationId.toByteArray(Charsets.UTF_8)).take(40)}"
        val (response, requestId) = post("/v1/potholes/report", body, idempotency)
        val pothole = response.optJSONObject("pothole")
            ?: throw CentralServiceException(502, "bad_service_response",
                "Central service returned no pothole [request ${requestId ?: "unknown"}]")
        val potholeId = pothole.optLong("id", -1)
        val seenCount = pothole.optInt("seen_count", -1)
        val duplicate = response.opt("duplicate") as? Boolean
        if (duplicate == null || potholeId <= 0 || seenCount < 1) {
            throw CentralServiceException(502, "bad_service_response",
                "Central service returned an invalid pothole [request ${requestId ?: "unknown"}]")
        }
        Log.i(TAG, "Pothole sync request_id=$requestId pothole_id=$potholeId duplicate=$duplicate")
        return PotholeSync(
            id = potholeId,
            duplicate = duplicate,
            seenCount = seenCount,
            requestId = requestId,
        )
    }

    fun resolveTender(
        lat: Double,
        lng: Double,
        operationId: String,
        addressHint: String? = null,
    ): TenderResolution {
        val body = JSONObject().put("lat", lat).put("lng", lng)
        normalizeCentralAddressHint(addressHint)?.let { body.put("address_hint", it) }
        val idempotency = "tender-${CentralServiceIdentity.sha256Hex(
            operationId.toByteArray(Charsets.UTF_8)
        ).take(40)}"
        val (response, requestId) = post("/v1/tenders/resolve", body, idempotency)
        val jurisdiction = response.opt("jurisdiction") as? JSONObject
        val tenderValue = response.opt("tender")
        val reasonValue = response.opt("reason")
        val tender = tenderValue as? JSONObject
        if (jurisdiction == null || !response.has("tender") || !response.has("reason")
            || (tenderValue !== JSONObject.NULL && tender == null)
            || (reasonValue !== JSONObject.NULL && reasonValue !is String)
            || (tender == null && (reasonValue !is String || reasonValue.isBlank()))) {
            throw CentralServiceException(502, "bad_service_response",
                "Central service returned an invalid tender result [request ${requestId ?: "unknown"}]")
        }
        val reason = nullableString(response, "reason")
        if (reason in setOf(
                "shared_credits_exhausted",
                "shared_budget_reached",
                "daily_vision_limit",
                "shared_vision_not_configured",
                "shared_vision_unavailable",
            )) {
            throw CentralServiceException(
                503,
                reason!!,
                "Tender matching is temporarily unavailable: $reason"
                    + (requestId?.let { " [request $it]" } ?: ""),
            )
        }
        val number = nullableString(tender, "tender_number")
        if (tender != null && number == null) {
            throw CentralServiceException(502, "bad_service_response",
                "Central service returned a tender without a number [request ${requestId ?: "unknown"}]")
        }
        val roadOwnership = nullableString(jurisdiction, "road_ownership")
        val allowedOwnership = setOf(
            "municipal",
            "national_highway",
            "state_highway",
            "district_highway",
            "rural",
            "outside_state",
        )
        if (roadOwnership !in allowedOwnership) {
            throw CentralServiceException(502, "bad_service_response",
                "Central service returned no authoritative road ownership " +
                    "[request ${requestId ?: "unknown"}]")
        }
        val terminalOwnership = roadOwnership != "municipal"
        if (terminalOwnership && (reason != roadOwnership || tender != null)) {
            throw CentralServiceException(502, "bad_service_response",
                "Central service returned an inconsistent ownership result " +
                    "[request ${requestId ?: "unknown"}]")
        }
        val contractor = nullableString(tender, "contractor")
        val published = nullableString(tender, "published")
        val note = number?.let {
            buildString {
                append("Probable contract: ").append(it)
                if (contractor != null) append(", ").append(contractor)
                if (published != null) append(", published ").append(published)
            }
        }
        Log.i(TAG, "Tender resolve request_id=$requestId tender=${number ?: "none"}")
        return TenderResolution(
            address = nullableString(jurisdiction, "address"),
            bodyLgd = nullableString(jurisdiction, "lgd"),
            bodyName = nullableString(jurisdiction, "town"),
            roadOwnership = roadOwnership!!,
            ownershipDetail = when (roadOwnership) {
                "national_highway", "state_highway", "district_highway" ->
                    nullableString(jurisdiction, "highway_name")
                "rural" -> nullableString(jurisdiction, "rural_body")
                else -> null
            },
            tenderNumber = number,
            contractor = contractor,
            tenderNote = note,
            reason = reason,
            requestId = requestId,
        )
    }

    /** Count a personal-key vision attempt without sending the key, image or location. */
    fun recordVisionActivity(captureMode: String, clientEventId: String): String? {
        require(captureMode in setOf("manual", "drive")) { "Bad capture mode" }
        val body = JSONObject()
            .put("event", "vision_check")
            .put("vision_provider", "personal_openai")
            .put("capture_mode", captureMode)
        val (_, requestId) = post("/v1/activity", body, "activity-$clientEventId")
        Log.i(TAG, "Personal vision activity request_id=$requestId capture_mode=$captureMode")
        return requestId
    }

}
