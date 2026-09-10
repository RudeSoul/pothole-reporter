package com.gauravsen.potholereporter.db.dao

import androidx.room.*
import com.gauravsen.potholereporter.db.entities.ReportEntity

/** Blob-free row used by frequent drive-mode spatial/deduplication scans. */
data class ReportMatchCandidate(
    val id: Long,
    val decision: String,
    val dedupe_eligible: Boolean,
    val capture_source: String,
    val source_event_key: String?,
    @ColumnInfo(name = "source_event_keys") val sourceEventKeys: String?,
    val drive_id: String?,
    @ColumnInfo(name = "sighting_drive_ids") val sightingDriveIds: String?,
    val damage_type: String?,
    val size: String?,
    val gps_accuracy: Float?,
    val speed_mps: Float?,
    val heading: Float?,
    val lat: Double?,
    val lng: Double?,
    val captured_at: Double?,
    val source_offset_s: Double?,
    val created_at: Double,
    val last_seen_at: Double?,
    val seen_count: Int,
)

@Dao
interface ReportDao {
    @Insert
    suspend fun insert(report: ReportEntity): Long

    @Update
    suspend fun update(report: ReportEntity): Int

    @Query("SELECT * FROM reports ORDER BY id DESC")
    suspend fun getAll(): List<ReportEntity>

    @Query("SELECT id FROM reports ORDER BY id DESC")
    suspend fun getAllIds(): List<Long>

    @Query("SELECT * FROM reports WHERE id = :id")
    suspend fun getById(id: Long): ReportEntity?

    @Query("SELECT photo FROM reports WHERE id = :id")
    suspend fun getThumbnail(id: Long): ByteArray?

    @Query("SELECT photo_full FROM reports WHERE id = :id")
    suspend fun getFullPhoto(id: Long): ByteArray?

    @Query("SELECT EXISTS(SELECT 1 FROM reports WHERE id = :id)")
    suspend fun exists(id: Long): Boolean

    @Query("SELECT * FROM reports WHERE server_pothole_id = :serverId ORDER BY id LIMIT 1")
    suspend fun getByServerPotholeId(serverId: Long): ReportEntity?

    @Query("""
        SELECT id FROM reports
        WHERE decision = 'accept'
          AND central_sync_eligible = 1
          AND (status = 'draft'
            OR (lat IS NOT NULL AND lng IS NOT NULL
              AND (tender_resolution_checked_at IS NULL
                OR (detection_provider = 'shared_server' AND road_ownership IS NULL)))
            OR (server_pothole_id IS NULL AND central_sync_error IS NULL))
        ORDER BY id
    """)
    suspend fun centralWorkIds(): List<Long>

    @Query("DELETE FROM reports WHERE id = :id")
    suspend fun deleteById(id: Long)

    @Query("SELECT * FROM reports WHERE drive_id = :driveId")
    suspend fun getByDriveId(driveId: String): List<ReportEntity>

    // Frequent drive scans deliberately omit all image BLOBs.
    @Query("""
        SELECT id,decision,dedupe_eligible,capture_source,
          source_event_key,source_event_keys,drive_id,sighting_drive_ids,
          damage_type,size,gps_accuracy,speed_mps,heading,lat,lng,captured_at,
          source_offset_s,created_at,last_seen_at,seen_count
        FROM reports
        WHERE decision = 'accept' AND lat BETWEEN :minLat AND :maxLat
    """)
    suspend fun findInLatBand(minLat: Double, maxLat: Double): List<ReportMatchCandidate>

    // For dedup: find reports from the same drive
    @Query("""
        SELECT id,decision,dedupe_eligible,capture_source,
          source_event_key,source_event_keys,drive_id,sighting_drive_ids,
          damage_type,size,gps_accuracy,speed_mps,heading,lat,lng,captured_at,
          source_offset_s,created_at,last_seen_at,seen_count
        FROM reports
        WHERE decision = 'accept' AND drive_id = :driveId
    """)
    suspend fun findAcceptedByDriveId(driveId: String): List<ReportMatchCandidate>

    // For dedup: find reports that have sighted this drive
    // Uses LIKE since sighting_drive_ids is a JSON array stored as text
    @Query("""
        SELECT id,decision,dedupe_eligible,capture_source,
          source_event_key,source_event_keys,drive_id,sighting_drive_ids,
          damage_type,size,gps_accuracy,speed_mps,heading,lat,lng,captured_at,
          source_offset_s,created_at,last_seen_at,seen_count
        FROM reports
        WHERE decision = 'accept' AND sighting_drive_ids LIKE '%' || :driveId || '%'
    """)
    suspend fun findBySightingDriveId(driveId: String): List<ReportMatchCandidate>

    @Query("SELECT COUNT(*) FROM reports")
    suspend fun count(): Int

    @Query("DELETE FROM reports")
    suspend fun deleteAll()
}
