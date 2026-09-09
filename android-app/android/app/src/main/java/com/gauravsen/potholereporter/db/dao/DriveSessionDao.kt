package com.gauravsen.potholereporter.db.dao

import androidx.room.*
import com.gauravsen.potholereporter.db.entities.DriveSessionEntity

@Dao
interface DriveSessionDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertOrUpdate(session: DriveSessionEntity)

    @Query("SELECT * FROM drive_sessions ORDER BY started_at DESC")
    suspend fun getAll(): List<DriveSessionEntity>

    @Query("SELECT * FROM drive_sessions WHERE id = :id")
    suspend fun getById(id: String): DriveSessionEntity?

    @Query("DELETE FROM drive_sessions")
    suspend fun deleteAll()
}
