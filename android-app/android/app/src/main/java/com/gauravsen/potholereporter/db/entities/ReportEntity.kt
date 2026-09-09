package com.gauravsen.potholereporter.db.entities

import androidx.room.*
import com.gauravsen.potholereporter.drivemode.LlmContractGenerated

/**
 * A detection report from native Drive Mode, equivalent to the IndexedDB
 * 'reports' store records in standalone.js.
 */
@Entity(
    tableName = "reports",
    indices = [
        Index(value = ["lat"]),
        Index(value = ["drive_id"]),
    ]
)
data class ReportEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,

    val assessment: String? = null,        // damaged, undamaged
    val image_quality: String? = null,     // acceptable, rejected
    val damage_type: String? = null,       // pothole_cavity, failed_patch, surface_breakup, etc.
    val size: String? = null,              // small, medium, large, null
    val description: String? = null,

    // Decision
    val decision: String = "reject",       // accept, reject, review
    val status: String = "rejected",       // draft, rejected, unrouted, queued, sent

    // Location
    val lat: Double? = null,
    val lng: Double? = null,
    val gps_accuracy: Float? = null,
    val speed_mps: Float? = null,
    val heading: Float? = null,
    val address: String? = null,

    // Image — stored as JPEG bytes
    val photo: ByteArray? = null,
    val photo_full: ByteArray? = null,     // full-res evidence copy

    // Drive context
    val drive_id: String? = null,
    val capture_source: String = "drive_live",
    val source_event_key: String? = null,
    @ColumnInfo(name = "source_event_keys") val sourceEventKeys: String? = null, // JSON array
    val captured_at: Double? = null,       // epoch seconds
    val source_offset_s: Double? = null,

    // Timestamps
    val created_at: Double = System.currentTimeMillis() / 1000.0,
    val last_seen_at: Double? = null,
    val seen_count: Int = 1,

    // Central civic record.
    val server_pothole_id: Long? = null,
    val server_duplicate: Boolean = false,
    // v1 records default to false during migration so old precise locations are
    // never uploaded retroactively without a fresh, disclosed capture.
    val central_sync_eligible: Boolean = true,
    val central_sync_error: String? = null,

    // Dedup tracking
    val dedupe_eligible: Boolean = true,
    @ColumnInfo(name = "sighting_drive_ids") val sightingDriveIds: String? = null, // JSON array
    @ColumnInfo(name = "event_sightings") val eventSightings: String? = null,      // JSON array

    // Detection metadata
    val detection_provider: String? = null,
    val detection_model: String? = null,
    val image_detail: String? = null,
    val prompt_version: String? = null,
    val schema_version: Int = LlmContractGenerated.DETECT_SCHEMA_VERSION,
    val evidence_count: Int = 1,

    // Email
    val email_subject: String? = null,
    val email_body: String? = null,
    val email_to: String? = null,
    val officer_title: String? = null,
    val body_lgd: String? = null,
    val body_name: String? = null,
    val tender_number: String? = null,
    val contractor: String? = null,
    val tender_note: String? = null,
    val tender_resolution_reason: String? = null,
    val tender_resolution_checked_at: Double? = null,
    val unrouted_reason: String? = null,
) {
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is ReportEntity) return false
        return id == other.id
    }
    override fun hashCode(): Int = id.hashCode()
}
