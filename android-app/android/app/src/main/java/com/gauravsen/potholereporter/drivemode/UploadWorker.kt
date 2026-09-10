package com.gauravsen.potholereporter.drivemode

import android.content.Context
import android.location.Geocoder
import android.util.Log
import androidx.work.*
import androidx.room.withTransaction
import com.gauravsen.potholereporter.db.AppDatabase
import com.gauravsen.potholereporter.db.entities.ReportEntity
import java.io.IOException
import java.util.Locale
import java.util.concurrent.TimeUnit

internal fun unroutedReasonForOwnership(ownership: String?): String? = when (ownership) {
    "national_highway", "state_highway", "district_highway" -> ownership
    "rural" -> "rural_road"
    "outside_state" -> "outside_area"
    else -> null
}

/**
 * Apply one authoritative central ownership result as a single Room update.
 *
 * Nullable municipal fields in a terminal response mean "not municipally owned", not
 * "keep the old value".  Clearing all cached recipient/email fields also prevents a
 * legacy draft from bypassing the WebView's fresh authority lookup.
 */
internal fun reportAfterTenderResolution(
    report: ReportEntity,
    resolution: CentralServiceClient.TenderResolution,
    checkedAt: Double,
    markReady: Boolean = true,
): ReportEntity {
    val unroutedReason = unroutedReasonForOwnership(resolution.roadOwnership)
    val terminalOwnership = unroutedReason != null
    val status = when {
        report.server_duplicate -> "duplicate"
        terminalOwnership -> "unrouted"
        markReady && report.status in setOf("draft", "unrouted") -> "queued"
        else -> report.status
    }
    return report.copy(
        address = resolution.address ?: report.address,
        body_lgd = if (terminalOwnership) null else resolution.bodyLgd,
        body_name = if (terminalOwnership) null else resolution.bodyName,
        road_ownership = resolution.roadOwnership,
        road_ownership_detail = resolution.ownershipDetail,
        email_subject = null,
        email_body = null,
        email_to = null,
        officer_title = null,
        tender_number = if (terminalOwnership) null else resolution.tenderNumber,
        contractor = if (terminalOwnership) null else resolution.contractor,
        tender_note = if (terminalOwnership) null else resolution.tenderNote,
        tender_resolution_reason = resolution.reason,
        tender_resolution_checked_at = checkedAt,
        unrouted_reason = unroutedReason,
        status = status,
    )
}

/** Fail closed while a central ownership retry is pending, clearing stale authority. */
internal fun reportAfterTenderResolutionFailure(
    report: ReportEntity,
    reason: String,
    checkedAt: Double?,
): ReportEntity = report.copy(
    body_lgd = null,
    body_name = null,
    road_ownership = null,
    road_ownership_detail = null,
    email_subject = null,
    email_body = null,
    email_to = null,
    officer_title = null,
    tender_number = null,
    contractor = null,
    tender_note = null,
    tender_resolution_reason = reason,
    tender_resolution_checked_at = checkedAt,
    unrouted_reason = "road_class_unknown",
    status = if (report.server_duplicate) "duplicate" else "unrouted",
)

/** A report's stable source event owns its tender retry idempotency key. */
internal fun tenderOperationId(report: ReportEntity): String =
    report.source_event_key?.takeIf { it.isNotBlank() }
        ?: "native-report:${report.id}"

/** A legacy shared row without persisted ownership never counts as resolved. */
internal fun reportNeedsOwnershipResolution(report: ReportEntity): Boolean =
    report.tender_resolution_checked_at == null
        || (report.detection_provider == "shared_server" && report.road_ownership == null)

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
            val deferredTenderReportIds = mutableSetOf<Long>()
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
                        detectionReceipt = observation.detection_receipt,
                        detectorProvider = observation.detector_provider,
                        model = observation.detector_model,
                        detail = observation.image_detail,
                        evidenceCount = observation.evidence_count,
                    )

                    // Resolve every server-confirmed duplicate at the candidate's exact
                    // coordinates before it can be folded into an older local row.
                    val candidateSnapshot = dao.getById(observation.report_id)
                    var mergeTender: CentralServiceClient.TenderResolution? = null
                    var mergeTenderError: Exception? = null
                    if (sync.duplicate && candidateSnapshot != null) {
                        try {
                            mergeTender = central.resolveTender(
                                lat = observation.lat,
                                lng = observation.lng,
                                operationId = observation.client_observation_id,
                                addressHint = candidateSnapshot.address,
                            )
                        } catch (e: Exception) {
                            mergeTenderError = e
                            if (isRetryable(e)) centralRetryNeeded = true
                            Log.w(
                                TAG,
                                "Central ownership check failed before duplicate merge " +
                                    "for report ${observation.report_id}",
                                e,
                            )
                        }
                    }
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
                            val error = mergeTenderError
                            if (error != null && isRetryable(error)) {
                                // Do not delete the only row that still carries this
                                // sighting's exact coordinates. Keep its outbox entry for
                                // the next idempotent retry, and fail-close both visible
                                // rows in the meantime.
                                val code = (error as? CentralServiceException)?.code
                                    ?: "invalid_tender_request"
                                val latestTarget = dao.getById(mergeTarget.id) ?: mergeTarget
                                dao.update(reportAfterTenderResolutionFailure(
                                    latestTarget,
                                    code,
                                    checkedAt = null,
                                ))
                                dao.update(reportAfterTenderResolutionFailure(
                                    candidate,
                                    code,
                                    checkedAt = null,
                                ))
                                deferredTenderReportIds.add(latestTarget.id)
                                deferredTenderReportIds.add(candidate.id)
                                return@withTransaction
                            }
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
                            var updated = latest.copy(
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
                            if (mergeTender != null) {
                                updated = reportAfterTenderResolution(
                                    updated,
                                    mergeTender!!,
                                    System.currentTimeMillis() / 1000.0,
                                )
                            } else if (mergeTenderError != null) {
                                val error = mergeTenderError!!
                                val code = (error as? CentralServiceException)?.code
                                    ?: "invalid_tender_request"
                                updated = reportAfterTenderResolutionFailure(
                                    updated,
                                    code,
                                    if (isRetryable(error)) null
                                    else System.currentTimeMillis() / 1000.0,
                                )
                            }
                            dao.update(updated)
                            // This cascades the candidate's outbox row. If the user
                            // already removed it, the existence check above returned.
                            dao.deleteById(candidate.id)
                        } else {
                            val latest = dao.getById(candidate.id) ?: return@withTransaction
                            val canonicalMismatch = latest.server_pothole_id != null
                                && latest.server_pothole_id != sync.id
                            var updated = latest.copy(
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
                            )
                            if (mergeTender != null) {
                                updated = reportAfterTenderResolution(
                                    updated,
                                    mergeTender!!,
                                    System.currentTimeMillis() / 1000.0,
                                )
                            } else if (mergeTenderError != null) {
                                val error = mergeTenderError!!
                                val code = (error as? CentralServiceException)?.code
                                    ?: "invalid_tender_request"
                                updated = reportAfterTenderResolutionFailure(
                                    updated,
                                    code,
                                    if (isRetryable(error)) null
                                    else System.currentTimeMillis() / 1000.0,
                                )
                            }
                            dao.update(updated)
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

            // A deferred duplicate must retry through its still-pending outbox row so
            // ownership uses that sighting's coordinates, not the older canonical row.
            val workIds = dao.centralWorkIds().filterNot(deferredTenderReportIds::contains)

            if (workIds.isEmpty() && !centralRetryNeeded) {
                Log.i(TAG, "No reports to process")
                return Result.success()
            }

            Log.i(TAG, "Processing ${workIds.size} reports")

            for (reportId in workIds) {
                var current = dao.getById(reportId) ?: continue
                var centralFailure = false
                try {
                    // Resolve once through Android's configured platform geocoder first.
                    // Passing that result as untrusted data lets the central service avoid
                    // another geocoder request; its operator-owned provider can still fill
                    // a missing hint.
                    if (current.address == null && current.lat != null && current.lng != null) {
                        val address = reverseGeocode(current.lat, current.lng)
                        if (address != null) {
                            val latest = dao.getById(current.id) ?: current
                            current = latest.copy(address = address)
                            dao.update(current)
                        }
                    }

                    if (current.lat != null && current.lng != null
                        && reportNeedsOwnershipResolution(current)) {
                        try {
                            val tender = central.resolveTender(
                                lat = current.lat,
                                lng = current.lng,
                                operationId = tenderOperationId(current),
                                addressHint = current.address,
                            )
                            val latest = dao.getById(current.id) ?: current
                            current = reportAfterTenderResolution(
                                latest,
                                tender,
                                System.currentTimeMillis() / 1000.0,
                            )
                            dao.update(current)
                        } catch (e: Exception) {
                            val retryable = isRetryable(e)
                            if (retryable) {
                                centralFailure = true
                                centralRetryNeeded = true
                            }
                            val code = (e as? CentralServiceException)?.code
                                ?: "invalid_tender_request"
                            val latest = dao.getById(current.id) ?: current
                            current = reportAfterTenderResolutionFailure(
                                latest,
                                code,
                                if (retryable) null
                                else System.currentTimeMillis() / 1000.0,
                            ).let { failed ->
                                if (retryable) failed
                                else failed.copy(central_sync_error = code)
                            }
                            dao.update(current)
                            Log.w(TAG, "Central tender retry failed for report ${current.id}", e)
                        }
                    }

                    if (current.server_duplicate) continue

                    // Tender resolution and any available address enrichment are done.
                    val latest = dao.getById(current.id) ?: current
                    dao.update(latest.copy(
                        status = if (latest.server_duplicate) "duplicate"
                            else if (latest.unrouted_reason != null) "unrouted"
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

    /** Use the device geocoder; native uploads never call the public Nominatim service. */
    @Suppress("DEPRECATION")
    private fun reverseGeocode(lat: Double, lng: Double): String? {
        return try {
            if (!Geocoder.isPresent()) return null
            val address = Geocoder(applicationContext, Locale.getDefault())
                .getFromLocation(lat, lng, 1)
                ?.firstOrNull() ?: return null
            val road = listOfNotNull(
                address.subThoroughfare?.takeIf { it.isNotBlank() },
                address.thoroughfare?.takeIf { it.isNotBlank() },
            ).joinToString(" ").takeIf { it.isNotBlank() }
            val parts = listOfNotNull(
                road,
                address.subLocality?.takeIf { it.isNotBlank() },
                address.locality?.takeIf { it.isNotBlank() }
                    ?: address.subAdminArea?.takeIf { it.isNotBlank() },
                address.postalCode?.takeIf { it.isNotBlank() },
            ).distinctBy { it.lowercase(Locale.ROOT) }

            if (parts.isNotEmpty()) parts.joinToString(", ")
            else address.getAddressLine(0)?.takeIf { it.isNotBlank() }
        } catch (e: Exception) {
            Log.w(TAG, "Platform reverse geocode failed", e)
            null
        }
    }
}
