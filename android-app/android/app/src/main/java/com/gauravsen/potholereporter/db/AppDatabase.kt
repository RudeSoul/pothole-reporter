package com.gauravsen.potholereporter.db

import android.content.Context
import androidx.room.*
import com.gauravsen.potholereporter.db.dao.DriveSessionDao
import com.gauravsen.potholereporter.db.dao.ReportDao
import com.gauravsen.potholereporter.db.dao.CentralObservationDao
import com.gauravsen.potholereporter.db.entities.CentralObservationEntity
import com.gauravsen.potholereporter.db.entities.DriveSessionEntity
import com.gauravsen.potholereporter.db.entities.ReportEntity

@Database(
    entities = [ReportEntity::class, DriveSessionEntity::class, CentralObservationEntity::class],
    version = 4,
    exportSchema = true
)
abstract class AppDatabase : RoomDatabase() {
    abstract fun reportDao(): ReportDao
    abstract fun driveSessionDao(): DriveSessionDao
    abstract fun centralObservationDao(): CentralObservationDao

    companion object {
        @Volatile
        private var INSTANCE: AppDatabase? = null

        fun getInstance(context: Context): AppDatabase {
            return INSTANCE ?: synchronized(this) {
                INSTANCE ?: Room.databaseBuilder(
                    context.applicationContext,
                    AppDatabase::class.java,
                    "pothole_reporter.db"
                ).addMigrations(MIGRATION_1_2, MIGRATION_2_3, MIGRATION_3_4)
                    .build()
                    .also { INSTANCE = it }
            }
        }

        @JvmField
        val MIGRATION_1_2 = object : androidx.room.migration.Migration(1, 2) {
            override fun migrate(db: androidx.sqlite.db.SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE reports ADD COLUMN server_pothole_id INTEGER")
                db.execSQL("ALTER TABLE reports ADD COLUMN server_duplicate INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE reports ADD COLUMN central_sync_eligible INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE reports ADD COLUMN central_sync_error TEXT")
                db.execSQL("ALTER TABLE reports ADD COLUMN condition_status TEXT NOT NULL DEFAULT 'open'")
                db.execSQL("ALTER TABLE reports ADD COLUMN condition_updated_at REAL")
                db.execSQL("ALTER TABLE reports ADD COLUMN condition_source TEXT")
                db.execSQL("ALTER TABLE reports ADD COLUMN condition_client_event_id TEXT")
                db.execSQL("ALTER TABLE reports ADD COLUMN condition_sync_pending INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE reports ADD COLUMN repair_description TEXT")
                db.execSQL("ALTER TABLE reports ADD COLUMN repair_photo BLOB")
                db.execSQL("ALTER TABLE reports ADD COLUMN repair_observed_at REAL")
                db.execSQL("ALTER TABLE reports ADD COLUMN repair_source_event_key TEXT")
                db.execSQL("ALTER TABLE reports ADD COLUMN repair_same_location_visible INTEGER")
                db.execSQL("ALTER TABLE reports ADD COLUMN repair_completed_repair_visible INTEGER")
                db.execSQL("ALTER TABLE reports ADD COLUMN repair_current_condition TEXT")
                db.execSQL("ALTER TABLE reports ADD COLUMN repair_assessment TEXT")
                db.execSQL("ALTER TABLE reports ADD COLUMN repair_image_quality TEXT")
                db.execSQL("ALTER TABLE reports ADD COLUMN repair_detection_model TEXT")
                db.execSQL("ALTER TABLE reports ADD COLUMN repair_prompt_version TEXT")
                db.execSQL("ALTER TABLE reports ADD COLUMN repair_schema_version INTEGER")
                db.execSQL("ALTER TABLE reports ADD COLUMN body_lgd TEXT")
                db.execSQL("ALTER TABLE reports ADD COLUMN tender_number TEXT")
                db.execSQL("ALTER TABLE reports ADD COLUMN contractor TEXT")
                db.execSQL("ALTER TABLE reports ADD COLUMN tender_note TEXT")
                db.execSQL("ALTER TABLE reports ADD COLUMN tender_resolution_reason TEXT")
                db.execSQL("ALTER TABLE reports ADD COLUMN tender_resolution_checked_at REAL")
                db.execSQL("ALTER TABLE reports ADD COLUMN detection_provider TEXT")
                db.execSQL("""
                    CREATE TABLE IF NOT EXISTS central_observation_outbox (
                      client_observation_id TEXT NOT NULL PRIMARY KEY,
                      report_id INTEGER NOT NULL,
                      observed_at_ms INTEGER NOT NULL,
                      lat REAL NOT NULL,
                      lng REAL NOT NULL,
                      gps_accuracy_m REAL,
                      heading_deg REAL,
                      speed_mps REAL,
                      damage_type TEXT NOT NULL,
                      size TEXT,
                      image_hash TEXT NOT NULL,
                      detector_provider TEXT NOT NULL,
                      detector_model TEXT NOT NULL,
                      image_detail TEXT NOT NULL,
                      evidence_count INTEGER NOT NULL,
                      drive_id TEXT,
                      local_match_report_id INTEGER,
                      local_match_kind TEXT,
                      attempt_count INTEGER NOT NULL,
                      last_error TEXT,
                      FOREIGN KEY(report_id) REFERENCES reports(id) ON UPDATE NO ACTION ON DELETE CASCADE
                    )
                """.trimIndent())
                db.execSQL("CREATE INDEX IF NOT EXISTS index_central_observation_outbox_report_id ON central_observation_outbox(report_id)")
            }
        }

        /** Retire repair/status updates while retaining the v2 columns for compatibility. */
        @JvmField
        val MIGRATION_2_3 = object : androidx.room.migration.Migration(2, 3) {
            override fun migrate(db: androidx.sqlite.db.SupportSQLiteDatabase) {
                db.execSQL(
                    """
                    UPDATE reports SET
                      condition_status = 'open',
                      condition_updated_at = NULL,
                      condition_source = NULL,
                      condition_client_event_id = NULL,
                      condition_sync_pending = 0,
                      repair_description = NULL,
                      repair_photo = NULL,
                      repair_observed_at = NULL,
                      repair_source_event_key = NULL,
                      repair_same_location_visible = NULL,
                      repair_completed_repair_visible = NULL,
                      repair_current_condition = NULL,
                      repair_assessment = NULL,
                      repair_image_quality = NULL,
                      repair_detection_model = NULL,
                      repair_prompt_version = NULL,
                      repair_schema_version = NULL
                    """.trimIndent(),
                )
            }
        }

        /**
         * Physically remove the retired detection-evidence and repair/status columns.
         *
         * The outbox is rebuilt alongside its parent so queued observations survive
         * the reports-table replacement and keep a valid cascading foreign key.
         */
        @JvmField
        val MIGRATION_3_4 = object : androidx.room.migration.Migration(3, 4) {
            override fun migrate(db: androidx.sqlite.db.SupportSQLiteDatabase) {
                db.execSQL(CREATE_REPORTS_V4)
                db.execSQL(CREATE_CENTRAL_OBSERVATION_OUTBOX_V4)

                db.execSQL(
                    """
                    INSERT INTO reports_v4 (
                      id,assessment,image_quality,damage_type,size,description,
                      decision,status,lat,lng,gps_accuracy,speed_mps,heading,address,
                      photo,photo_full,drive_id,capture_source,source_event_key,
                      source_event_keys,captured_at,source_offset_s,created_at,last_seen_at,
                      seen_count,server_pothole_id,server_duplicate,central_sync_eligible,
                      central_sync_error,dedupe_eligible,sighting_drive_ids,event_sightings,
                      detection_provider,detection_model,image_detail,prompt_version,
                      schema_version,evidence_count,email_subject,email_body,email_to,
                      officer_title,body_lgd,body_name,tender_number,contractor,tender_note,
                      tender_resolution_reason,tender_resolution_checked_at,unrouted_reason
                    )
                    SELECT
                      id,assessment,image_quality,damage_type,size,description,
                      decision,status,lat,lng,gps_accuracy,speed_mps,heading,address,
                      photo,photo_full,drive_id,capture_source,source_event_key,
                      source_event_keys,captured_at,source_offset_s,created_at,last_seen_at,
                      seen_count,server_pothole_id,server_duplicate,central_sync_eligible,
                      central_sync_error,dedupe_eligible,sighting_drive_ids,event_sightings,
                      detection_provider,detection_model,image_detail,prompt_version,
                      schema_version,evidence_count,email_subject,email_body,email_to,
                      officer_title,body_lgd,body_name,tender_number,contractor,tender_note,
                      tender_resolution_reason,tender_resolution_checked_at,unrouted_reason
                    FROM reports
                    """.trimIndent(),
                )
                db.execSQL(
                    """
                    INSERT INTO central_observation_outbox_v4 (
                      client_observation_id,report_id,observed_at_ms,lat,lng,
                      gps_accuracy_m,heading_deg,speed_mps,damage_type,size,image_hash,
                      detector_provider,detector_model,image_detail,evidence_count,drive_id,
                      local_match_report_id,local_match_kind,attempt_count,last_error
                    )
                    SELECT
                      client_observation_id,report_id,observed_at_ms,lat,lng,
                      gps_accuracy_m,heading_deg,speed_mps,damage_type,size,image_hash,
                      detector_provider,detector_model,image_detail,evidence_count,drive_id,
                      local_match_report_id,local_match_kind,attempt_count,last_error
                    FROM central_observation_outbox
                    """.trimIndent(),
                )

                db.execSQL("DROP TABLE central_observation_outbox")
                db.execSQL("DROP TABLE reports")
                db.execSQL("ALTER TABLE reports_v4 RENAME TO reports")
                db.execSQL(CREATE_CENTRAL_OBSERVATION_OUTBOX_FINAL)
                db.execSQL(
                    """
                    INSERT INTO central_observation_outbox (
                      client_observation_id,report_id,observed_at_ms,lat,lng,
                      gps_accuracy_m,heading_deg,speed_mps,damage_type,size,image_hash,
                      detector_provider,detector_model,image_detail,evidence_count,drive_id,
                      local_match_report_id,local_match_kind,attempt_count,last_error
                    )
                    SELECT
                      client_observation_id,report_id,observed_at_ms,lat,lng,
                      gps_accuracy_m,heading_deg,speed_mps,damage_type,size,image_hash,
                      detector_provider,detector_model,image_detail,evidence_count,drive_id,
                      local_match_report_id,local_match_kind,attempt_count,last_error
                    FROM central_observation_outbox_v4
                    """.trimIndent(),
                )
                db.execSQL("DROP TABLE central_observation_outbox_v4")
                db.execSQL("CREATE INDEX IF NOT EXISTS index_reports_lat ON reports(lat)")
                db.execSQL("CREATE INDEX IF NOT EXISTS index_reports_drive_id ON reports(drive_id)")
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS index_central_observation_outbox_report_id " +
                        "ON central_observation_outbox(report_id)",
                )
            }
        }

        private val CREATE_REPORTS_V4 = """
            CREATE TABLE IF NOT EXISTS reports_v4 (
              id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
              assessment TEXT,
              image_quality TEXT,
              damage_type TEXT,
              size TEXT,
              description TEXT,
              decision TEXT NOT NULL,
              status TEXT NOT NULL,
              lat REAL,
              lng REAL,
              gps_accuracy REAL,
              speed_mps REAL,
              heading REAL,
              address TEXT,
              photo BLOB,
              photo_full BLOB,
              drive_id TEXT,
              capture_source TEXT NOT NULL,
              source_event_key TEXT,
              source_event_keys TEXT,
              captured_at REAL,
              source_offset_s REAL,
              created_at REAL NOT NULL,
              last_seen_at REAL,
              seen_count INTEGER NOT NULL,
              server_pothole_id INTEGER,
              server_duplicate INTEGER NOT NULL,
              central_sync_eligible INTEGER NOT NULL,
              central_sync_error TEXT,
              dedupe_eligible INTEGER NOT NULL,
              sighting_drive_ids TEXT,
              event_sightings TEXT,
              detection_provider TEXT,
              detection_model TEXT,
              image_detail TEXT,
              prompt_version TEXT,
              schema_version INTEGER NOT NULL,
              evidence_count INTEGER NOT NULL,
              email_subject TEXT,
              email_body TEXT,
              email_to TEXT,
              officer_title TEXT,
              body_lgd TEXT,
              body_name TEXT,
              tender_number TEXT,
              contractor TEXT,
              tender_note TEXT,
              tender_resolution_reason TEXT,
              tender_resolution_checked_at REAL,
              unrouted_reason TEXT
            )
        """.trimIndent()

        // Use a constraint-free staging table so this migration does not depend on
        // SQLite's version-specific foreign-key rewrite behaviour during table rename.
        private val CREATE_CENTRAL_OBSERVATION_OUTBOX_V4 = """
            CREATE TABLE IF NOT EXISTS central_observation_outbox_v4 (
              client_observation_id TEXT NOT NULL,
              report_id INTEGER NOT NULL,
              observed_at_ms INTEGER NOT NULL,
              lat REAL NOT NULL,
              lng REAL NOT NULL,
              gps_accuracy_m REAL,
              heading_deg REAL,
              speed_mps REAL,
              damage_type TEXT NOT NULL,
              size TEXT,
              image_hash TEXT NOT NULL,
              detector_provider TEXT NOT NULL,
              detector_model TEXT NOT NULL,
              image_detail TEXT NOT NULL,
              evidence_count INTEGER NOT NULL,
              drive_id TEXT,
              local_match_report_id INTEGER,
              local_match_kind TEXT,
              attempt_count INTEGER NOT NULL,
              last_error TEXT,
              PRIMARY KEY(client_observation_id)
            )
        """.trimIndent()

        private val CREATE_CENTRAL_OBSERVATION_OUTBOX_FINAL = """
            CREATE TABLE IF NOT EXISTS central_observation_outbox (
              client_observation_id TEXT NOT NULL,
              report_id INTEGER NOT NULL,
              observed_at_ms INTEGER NOT NULL,
              lat REAL NOT NULL,
              lng REAL NOT NULL,
              gps_accuracy_m REAL,
              heading_deg REAL,
              speed_mps REAL,
              damage_type TEXT NOT NULL,
              size TEXT,
              image_hash TEXT NOT NULL,
              detector_provider TEXT NOT NULL,
              detector_model TEXT NOT NULL,
              image_detail TEXT NOT NULL,
              evidence_count INTEGER NOT NULL,
              drive_id TEXT,
              local_match_report_id INTEGER,
              local_match_kind TEXT,
              attempt_count INTEGER NOT NULL,
              last_error TEXT,
              PRIMARY KEY(client_observation_id),
              FOREIGN KEY(report_id) REFERENCES reports(id)
                ON UPDATE NO ACTION ON DELETE CASCADE
            )
        """.trimIndent()
    }
}
