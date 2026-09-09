package com.gauravsen.potholereporter.drivemode

import android.content.Context
import android.util.Log
import androidx.work.*
import androidx.room.withTransaction
import com.gauravsen.potholereporter.db.AppDatabase
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * WorkManager worker that processes reports with status "draft".
 *
 * This worker synchronizes accepted reports and resolves central
 * tender/address metadata so they are ready for review. The WebView
 * performs final authority routing and opens the sole complaint path,
 * a pre-addressed email draft, when the user taps Email complaint.
 *
 * Scheduling: enqueued once when DriveModeService stops, with
 * network connectivity constraint. Retries with exponential backoff.
 */
class UploadWorker(
    appContext: Context,
    params: WorkerParameters
) : CoroutineWorker(appContext, params) {

    companion object {
        private const val TAG = "UploadWorker"
        const val WORK_NAME = "pothole_report_upload"

        private fun isRetryable(error: Exception): Boolean = when (error) {
            is CentralServiceException -> error.retryable
            is IOException -> true
            else -> false
        }

        /**
         * Enqueue a one-time upload job with network constraint.
         */
        fun enqueue(context: Context, ensureAfterCurrent: Boolean = false) {
            val constraints = Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build()

            val request = OneTimeWorkRequestBuilder<UploadWorker>()
                .setConstraints(constraints)
                .setBackoffCriteria(
                    BackoffPolicy.EXPONENTIAL,
                    10_000L,
                    TimeUnit.MILLISECONDS
                )
                .build()

            WorkManager.getInstance(context)
                .enqueueUniqueWork(
                    WORK_NAME,
                    if (ensureAfterCurrent) ExistingWorkPolicy.APPEND_OR_REPLACE
                    else ExistingWorkPolicy.KEEP,
                    request
                )

            Log.i(TAG, "Upload work enqueued")
        }
    }

    override suspend fun doWork(): Result {
        Log.i(TAG, "Starting report processing")

        return try {
            val db = AppDatabase.getInstance(applicationContext)
            val dao = db.reportDao()
            val observationDao = db.centralObservationDao()
            val serviceUrl = applicationContext
                .getSharedPreferences(CentralServiceIdentity.PREFS, Context.MODE_PRIVATE)
                .getString("service_url", CentralServiceIdentity.DEFAULT_SERVICE_URL)
                ?: CentralServiceIdentity.DEFAULT_SERVICE_URL
            val central = CentralServiceClient(serviceUrl, CentralServiceIdentity(applicationContext))
            var centralRetryNeeded = false
            val deduper = DuplicateDetector(dao)

            // Each lightweight outbox row points at one provisional ReportEntity, where
            // its evidence is stored exactly once. Apply responses only while both rows
            // still exist so deleting a report during a request can never resurrect it.
            for (observation in observationDao.pending()) {
                if (!dao.exists(observation.report_id)) {
                    observationDao.delete(observation.client_observation_id)
                    continue
                }
                try {
                    val sync = central.reportPothole(
                        clientObservationId = observation.client_observation_id,
                        observedAtMs = observation.observed_at_ms,
                        lat = observation.lat,
                        lng = observation.lng,
                        gpsAccuracy = observation.gps_accuracy_m,
                        heading = observation.heading_deg,
                        speed = observation.speed_mps,
                        damageType = observation.damage_type,
                        size = observation.size,
                        imageHash = observation.image_hash,
                        detectorProvider = observation.detector_provider,
                        model = observation.detector_model,
                        detail = observation.image_detail,
                        evidenceCount = observation.evidence_count,
                    )
                    db.withTransaction {
                        if (!observationDao.exists(observation.client_observation_id)) {
                            return@withTransaction
                        }
                        val candidate = dao.getById(observation.report_id)
                        if (candidate == null) {
                            observationDao.delete(observation.client_observation_id)
                            return@withTransaction
                        }

                        val canonical = dao.getByServerPotholeId(sync.id)
                        val hintedTarget = observation.local_match_report_id
                            ?.let { dao.getById(it) }
                            ?.takeIf { target ->
                                target.id != candidate.id
                                    && (target.server_pothole_id == null
                                        || target.server_pothole_id == sync.id)
                            }
                        val mergeTarget = if (sync.duplicate) {
                            canonical?.takeIf { it.id != candidate.id } ?: hintedTarget
                        } else null

                        if (mergeTarget != null) {
                            val merged = deduper.mergeDuplicate(
                                candidate,
                                DuplicateDetector.DuplicateMatch(
                                    mergeTarget,
                                    observation.local_match_kind ?: "prior_drive",
                                ),
                            )
                            val latest = dao.getById(merged.id) ?: merged
                            val canonicalMismatch = latest.server_pothole_id != null
                                && latest.server_pothole_id != sync.id
                            val adoptingCanonical = latest.server_pothole_id == null
                            val updated = latest.copy(
                                server_pothole_id = if (canonicalMismatch) {
                                    latest.server_pothole_id
                                } else sync.id,
                                server_duplicate = if (adoptingCanonical) sync.duplicate
                                    else latest.server_duplicate,
                                central_sync_eligible = true,
                                central_sync_error = null,
                                seen_count = sync.seenCount,
                                status = if (adoptingCanonical && sync.duplicate
                                    && latest.status == "draft") "duplicate"
                                    else latest.status,
                            )
                            dao.update(updated)
                            // This cascades the candidate's outbox row. If the user
                            // already removed it, the existence check above returned.
                            dao.deleteById(candidate.id)
                        } else {
                            val latest = dao.getById(candidate.id) ?: return@withTransaction
                            val canonicalMismatch = latest.server_pothole_id != null
                                && latest.server_pothole_id != sync.id
                            dao.update(latest.copy(
                                // Never replace an existing canonical ID with a different
                                // one. A mismatch means this provisional row represents a
                                // separate recurrence/cross-device canonical record.
                                server_pothole_id = if (canonicalMismatch) {
                                    latest.server_pothole_id
                                } else sync.id,
                                server_duplicate = if (canonicalMismatch) {
                                    latest.server_duplicate
                                } else sync.duplicate,
                                central_sync_error = null,
                                seen_count = sync.seenCount,
                                status = if (canonicalMismatch) latest.status
                                    else if (sync.duplicate) "duplicate"
                                    else if (latest.status == "duplicate") "draft"
                                    else latest.status,
                            ))
                            observationDao.delete(observation.client_observation_id)
                        }
                    }
                } catch (e: Exception) {
                    if (isRetryable(e)) {
                        observationDao.markAttempt(observation.client_observation_id, null)
                        centralRetryNeeded = true
                    } else {
                        val code = (e as? CentralServiceException)?.code ?: "invalid_request"
                        observationDao.markAttempt(observation.client_observation_id, code)
                        dao.getById(observation.report_id)?.let { latest ->
                            dao.update(latest.copy(central_sync_error = code))
                        }
                    }
                    Log.w(TAG, "Central observation retry failed for report ${observation.report_id}", e)
                }
            }

            val workIds = dao.centralWorkIds()

            if (workIds.isEmpty() && !centralRetryNeeded) {
                Log.i(TAG, "No reports to process")
                return Result.success()
            }

            Log.i(TAG, "Processing ${workIds.size} reports")

            for (reportId in workIds) {
                var current = dao.getById(reportId) ?: continue
                var centralFailure = false
                try {
                    if (current.lat != null && current.lng != null
                        && current.tender_resolution_checked_at == null) {
                        try {
                            val tender = central.resolveTender(
                                current.lat,
                                current.lng,
                                current.source_event_key ?: "native-report:${current.id}",
                            )
                            val latest = dao.getById(current.id) ?: current
                            current = latest.copy(
                                address = tender.address ?: latest.address,
                                body_lgd = tender.bodyLgd ?: latest.body_lgd,
                                body_name = tender.bodyName ?: latest.body_name,
                                tender_number = tender.tenderNumber,
                                contractor = tender.contractor,
                                tender_note = tender.tenderNote,
                                tender_resolution_reason = tender.reason,
                                tender_resolution_checked_at = System.currentTimeMillis() / 1000.0,
                            )
                            dao.update(current)
                        } catch (e: Exception) {
                            if (isRetryable(e)) {
                                centralFailure = true
                                centralRetryNeeded = true
                            } else {
                                val code = (e as? CentralServiceException)?.code
                                    ?: "invalid_tender_request"
                                val latest = dao.getById(current.id) ?: current
                                current = latest.copy(
                                    central_sync_error = code,
                                    tender_resolution_reason = code,
                                    tender_resolution_checked_at = System.currentTimeMillis() / 1000.0,
                                )
                                dao.update(current)
                            }
                            Log.w(TAG, "Central tender retry failed for report ${current.id}", e)
                        }
                    }

                    if (current.server_duplicate) continue

                    // Reverse geocode if address is missing
                    if (current.address == null && current.lat != null && current.lng != null) {
                        val address = reverseGeocode(current.lat, current.lng)
                        if (address != null) {
                            val latest = dao.getById(current.id) ?: current
                            dao.update(latest.copy(
                                address = address,
                                status = if (centralFailure) "draft" else "queued",
                            ))
                            Log.d(TAG, "Report ${current.id} geocoded; central_sync_pending=$centralFailure")
                            continue
                        }
                    }

                    // If already has address or geocoding failed, just mark as queued
                    val latest = dao.getById(current.id) ?: current
                    dao.update(latest.copy(
                        status = if (latest.server_duplicate) "duplicate"
                            else if (centralFailure) "draft" else "queued",
                    ))
                    Log.d(TAG, "Report ${current.id} processing complete; central_sync_pending=$centralFailure")
                } catch (e: Exception) {
                    Log.w(TAG, "Failed to process report ${current.id}", e)
                    // Continue with other reports
                }
            }

            if (centralRetryNeeded) Result.retry() else Result.success()
        } catch (e: Exception) {
            Log.e(TAG, "Upload worker failed", e)
            Result.retry()
        }
    }

    /**
     * Simple reverse geocode using Nominatim, matching the web client's format.
     */
    private fun reverseGeocode(lat: Double, lng: Double): String? {
        return try {
            val url = java.net.URL(
                "https://nominatim.openstreetmap.org/reverse?lat=$lat&lon=$lng&format=jsonv2&zoom=17&addressdetails=1"
            )
            val conn = url.openConnection() as java.net.HttpURLConnection
            conn.connectTimeout = 12000
            conn.readTimeout = 12000
            conn.setRequestProperty("User-Agent", "PotholeReporter/1.0")

            if (conn.responseCode != 200) return null

            val json = org.json.JSONObject(conn.inputStream.bufferedReader().readText())
            val address = json.optJSONObject("address") ?: return null

            fun value(key: String): String? = if (address.isNull(key)) null
                else address.optString(key).takeIf { it.isNotBlank() }
            val parts = listOfNotNull(
                value("road") ?: value("pedestrian") ?: value("residential")
                    ?: value("footway"),
                value("neighbourhood") ?: value("hamlet"),
                value("suburb") ?: value("village"),
                value("city") ?: value("town") ?: value("municipality"),
                value("postcode"),
            ).distinct()

            if (parts.isNotEmpty()) parts.joinToString(", ") else null
        } catch (e: Exception) {
            Log.w(TAG, "Reverse geocode failed", e)
            null
        }
    }
}
