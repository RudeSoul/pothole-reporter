package com.gauravsen.potholereporter.drivemode

import com.gauravsen.potholereporter.db.dao.ReportDao
import com.gauravsen.potholereporter.db.dao.ReportMatchCandidate
import com.gauravsen.potholereporter.db.entities.ReportEntity
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.abs
import kotlin.math.max

/**
 * Replicates the web's roadEventMatch() and deduplication logic,
 * checking against Room database for duplicate reports.
 */
class DuplicateDetector(private val reportDao: ReportDao) {
    private val writeMutex = Mutex()

    data class DuplicateMatch(
        val priorReport: ReportEntity,
        val kind: String,   // "same_source", "same_drive", "prior_drive"
    )

    suspend fun findDuplicate(candidate: ReportEntity): DuplicateMatch? {
        if (candidate.decision != "accept" || !candidate.dedupe_eligible) return null
        if (candidate.capture_source != "drive_live") return null // Manual captures not suppressed

        val driveId = candidate.drive_id ?: return null

        // 1. Check reports that have sighted this drive_id
        val sightingsMatches = reportDao.findBySightingDriveId(driveId)
        for (prior in sightingsMatches) {
            val matchKind = roadEventMatch(candidate, prior)
            if (matchKind != null) {
                val full = reportDao.getById(prior.id) ?: continue
                return DuplicateMatch(full, matchKind)
            }
        }

        // 2. Check reports from this drive_id
        val sameDriveMatches = reportDao.findAcceptedByDriveId(driveId)
        for (prior in sameDriveMatches) {
            val matchKind = roadEventMatch(candidate, prior)
            if (matchKind != null) {
                val full = reportDao.getById(prior.id) ?: continue
                return DuplicateMatch(full, matchKind)
            }
        }

        // 3. Check reports in a latitude band
        // DEDUPE_HISTORY_RADIUS_M = 15m. 15 / 110900 = 0.000135
        val lat = candidate.lat ?: return null
        val radiusDeg = 15.0 / 110900.0
        val historyMatches = reportDao.findInLatBand(lat - radiusDeg, lat + radiusDeg)
        for (prior in historyMatches) {
            val matchKind = roadEventMatch(candidate, prior)
            if (matchKind != null) {
                val full = reportDao.getById(prior.id) ?: continue
                return DuplicateMatch(full, matchKind)
            }
        }

        return null
    }

    /**
     * Persist every accepted sighting as a provisional row. The central service is
     * authoritative for cross-device deduplication; keeping the candidate until its
     * response arrives preserves the evidence until that decision is known.
     */
    suspend fun insertPending(report: ReportEntity): Pair<Long, DuplicateMatch?> =
        writeMutex.withLock {
            val duplicateMatch = findDuplicate(report)
            val id = reportDao.insert(report)
            Pair(id, duplicateMatch)
        }

    /** Apply a duplicate only after the central service confirms the canonical match. */
    suspend fun mergeDuplicate(report: ReportEntity, match: DuplicateMatch): ReportEntity =
        writeMutex.withLock {
            val prior = reportDao.getById(match.priorReport.id) ?: match.priorReport
            val exactReplay = match.kind == "same_source"
            val sourceKeys = stringArray(prior.sourceEventKeys).apply {
                prior.source_event_key?.let { if (!contains(it)) add(it) }
                report.source_event_key?.let { if (!contains(it)) add(it) }
            }.takeLast(64)
            val driveIds = stringArray(prior.sightingDriveIds).apply {
                prior.drive_id?.let { if (!contains(it)) add(it) }
                report.drive_id?.let { if (!contains(it)) add(it) }
            }
            val sightings = jsonArray(prior.eventSightings)
            if (!exactReplay && sightings.length() < 64) sightings.put(eventSighting(report))
            val reportTime = report.last_seen_at ?: report.captured_at ?: report.created_at
            val priorTime = prior.last_seen_at ?: prior.captured_at ?: prior.created_at
            val merged = prior.copy(
                sourceEventKeys = JSONArray(sourceKeys).toString(),
                sightingDriveIds = JSONArray(driveIds).toString(),
                eventSightings = sightings.toString(),
                seen_count = if (exactReplay) prior.seen_count else prior.seen_count + 1,
                last_seen_at = max(priorTime, reportTime),
            )
            reportDao.update(merged)
            merged
        }

    private fun stringArray(value: String?): MutableList<String> {
        if (value.isNullOrBlank()) return mutableListOf()
        return runCatching {
            val array = JSONArray(value)
            MutableList(array.length()) { index -> array.optString(index) }
                .filter { it.isNotBlank() }.distinct().toMutableList()
        }.getOrDefault(mutableListOf())
    }

    private fun jsonArray(value: String?): JSONArray = runCatching {
        if (value.isNullOrBlank()) JSONArray() else JSONArray(value)
    }.getOrDefault(JSONArray())

    private fun eventSighting(report: ReportEntity): JSONObject = JSONObject()
        .put("drive_id", report.drive_id ?: JSONObject.NULL)
        .put("lat", report.lat ?: JSONObject.NULL)
        .put("lng", report.lng ?: JSONObject.NULL)
        .put("source_offset_s", report.source_offset_s ?: JSONObject.NULL)
        .put("captured_at", report.captured_at ?: JSONObject.NULL)
        .put("gps_accuracy", report.gps_accuracy ?: JSONObject.NULL)
        .put("speed_mps", report.speed_mps ?: JSONObject.NULL)
        .put("heading", report.heading ?: JSONObject.NULL)
        .put("source_event_key", report.source_event_key ?: JSONObject.NULL)

    private fun roadEventMatch(candidate: ReportEntity, prior: ReportMatchCandidate): String? {
        if (prior.decision != "accept" || !prior.dedupe_eligible) return null
        if (candidate.capture_source != "drive_live" || prior.capture_source != "drive_live") return null

        // Same source match
        if (candidate.source_event_key != null && candidate.source_event_key == prior.source_event_key) {
            return "same_source"
        }

        // Damage type compatibility
        if (candidate.damage_type != prior.damage_type) {
            val cavityTypes = setOf("pothole_cavity", "failed_patch")
            if (candidate.damage_type !in cavityTypes || prior.damage_type !in cavityTypes) {
                return null
            }
        }

        // Size conflict (small vs large)
        if ((candidate.size == "small" && prior.size == "large") ||
            (candidate.size == "large" && prior.size == "small")) {
            return null
        }

        val isSameDrive = candidate.drive_id == prior.drive_id

        if (isSameDrive) {
            if (matchesEverySameDriveSighting(candidate, prior)) {
                return "same_drive"
            }
            return null
        } else {
            // Different drive logic
            val candAcc = candidate.gps_accuracy ?: 999f
            val priorAcc = prior.gps_accuracy ?: 999f
            if (candAcc > 15f || priorAcc > 15f) return null

            val candTime = candidate.captured_at ?: 0.0
            val priorTime = prior.captured_at ?: 0.0
            val timeDiffDays = abs(candTime - priorTime) / 86400.0
            if (timeDiffDays > 30.0) return null

            val lat1 = candidate.lat ?: return null
            val lng1 = candidate.lng ?: return null
            val lat2 = prior.lat ?: return null
            val lng2 = prior.lng ?: return null
            val dist = distMeters(lat1, lng1, lat2, lng2)

            val candHead = candidate.heading
            val priorHead = prior.heading

            if (candHead == null || priorHead == null) {
                if (dist <= 5.0) return "prior_drive"
                return null
            }

            if (dist > 8.0) return null

            var headDiff = abs(candHead - priorHead)
            if (headDiff > 180f) headDiff = 360f - headDiff
            if (headDiff > 45f) return null

            return "prior_drive"
        }
    }

    private fun matchesEverySameDriveSighting(
        candidate: ReportEntity,
        prior: ReportMatchCandidate,
    ): Boolean {
        // Temporal proximity (4s) and spatial proximity (12m)
        val candLat = candidate.lat ?: return false
        val candLng = candidate.lng ?: return false
        val priorLat = prior.lat ?: return false
        val priorLng = prior.lng ?: return false

        val dist = distMeters(candLat, candLng, priorLat, priorLng)
        if (dist > 12.0) return false

        val candTime = candidate.source_offset_s ?: candidate.captured_at ?: 0.0
        val priorTime = prior.source_offset_s ?: prior.captured_at ?: 0.0

        val timeDiff = abs(candTime - priorTime)
        if (timeDiff > 4.0) return false

        return true
    }

    private fun distMeters(lat1: Double, lng1: Double, lat2: Double, lng2: Double): Double {
        val r = 6371000.0
        val rad = Math.PI / 180.0
        val dLat = (lat2 - lat1) * rad
        val dLng = (lng2 - lng1) * rad
        val a = Math.sin(dLat / 2).let { it * it } +
            Math.cos(lat1 * rad) * Math.cos(lat2 * rad) *
            Math.sin(dLng / 2).let { it * it }
        return 2 * r * Math.asin(Math.sqrt(a))
    }
}
