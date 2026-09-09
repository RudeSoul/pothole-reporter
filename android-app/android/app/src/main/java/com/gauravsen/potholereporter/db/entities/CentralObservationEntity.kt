package com.gauravsen.potholereporter.db.entities

import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index
import androidx.room.PrimaryKey

/** Durable local outbox entry for one accepted central pothole sighting. */
@Entity(
    tableName = "central_observation_outbox",
    foreignKeys = [ForeignKey(
        entity = ReportEntity::class,
        parentColumns = ["id"],
        childColumns = ["report_id"],
        onDelete = ForeignKey.CASCADE,
    )],
    indices = [Index("report_id")],
)
data class CentralObservationEntity(
    @PrimaryKey val client_observation_id: String,
    val report_id: Long,
    val observed_at_ms: Long,
    val lat: Double,
    val lng: Double,
    val gps_accuracy_m: Float?,
    val heading_deg: Float?,
    val speed_mps: Float?,
    val damage_type: String,
    val size: String?,
    val image_hash: String,
    val detector_provider: String,
    val detector_model: String,
    val image_detail: String,
    val evidence_count: Int,
    val drive_id: String?,
    val local_match_report_id: Long?,
    val local_match_kind: String?,
    val attempt_count: Int = 0,
    val last_error: String? = null,
)
