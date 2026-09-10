package com.gauravsen.potholereporter.db.dao

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import com.gauravsen.potholereporter.db.entities.CentralObservationEntity

@Dao
interface CentralObservationDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun enqueue(observation: CentralObservationEntity)

    @Query("SELECT * FROM central_observation_outbox WHERE last_error IS NULL ORDER BY observed_at_ms")
    suspend fun pending(): List<CentralObservationEntity>

    @Query("SELECT EXISTS(SELECT 1 FROM central_observation_outbox WHERE client_observation_id = :id)")
    suspend fun exists(id: String): Boolean

    @Query("""
        SELECT EXISTS(
          SELECT 1 FROM central_observation_outbox
          WHERE last_error IS NULL
            AND (report_id = :reportId OR local_match_report_id = :reportId)
        )
    """)
    suspend fun hasPendingForReport(reportId: Long): Boolean

    @Query("DELETE FROM central_observation_outbox WHERE client_observation_id = :id")
    suspend fun delete(id: String)

    @Query("DELETE FROM central_observation_outbox")
    suspend fun deleteAll()

    @Query("UPDATE central_observation_outbox SET attempt_count = attempt_count + 1, last_error = :error WHERE client_observation_id = :id")
    suspend fun markAttempt(id: String, error: String?)
}
