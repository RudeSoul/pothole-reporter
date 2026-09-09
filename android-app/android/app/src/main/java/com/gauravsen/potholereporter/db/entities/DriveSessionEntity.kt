package com.gauravsen.potholereporter.db.entities

import androidx.room.*

@Entity(tableName = "drive_sessions")
data class DriveSessionEntity(
    @PrimaryKey val id: String,
    val started_at: Double? = null,
    val ended_at: Double? = null,
    val checked: Int = 0,
    val found: Int = 0,
    val already: Int = 0,
    val already_ids: String? = null,  // JSON array of strings
    val gps_track: String? = null,    // JSON array of [lat, lng, ts] arrays
)
