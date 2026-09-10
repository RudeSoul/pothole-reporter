package com.gauravsen.potholereporter.db

import android.content.Context
import androidx.room.testing.MigrationTestHelper
import androidx.sqlite.db.SupportSQLiteDatabase
import androidx.sqlite.db.SupportSQLiteOpenHelper
import androidx.sqlite.db.framework.FrameworkSQLiteOpenHelperFactory
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class AppDatabaseMigrationTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context: Context = instrumentation.targetContext

    @get:Rule
    val migrationHelper = MigrationTestHelper(instrumentation, AppDatabase::class.java)

    @After
    fun deleteDatabase() {
        context.deleteDatabase(TEST_DATABASE)
    }

    @Test
    fun migrate1To6_preservesLegacyRowsAndUsesPrivacySafeDefaults() {
        createVersionOneDatabase().use { database ->
            insertLegacyReport(database)
        }

        migrationHelper.runMigrationsAndValidate(
            TEST_DATABASE,
            6,
            true,
            AppDatabase.MIGRATION_1_2,
            AppDatabase.MIGRATION_2_3,
            AppDatabase.MIGRATION_3_4,
            AppDatabase.MIGRATION_4_5,
            AppDatabase.MIGRATION_5_6,
        ).use { database ->
            database.query(
                """
                SELECT id,damage_type,lat,lng,length(photo),length(photo_full),body_name,
                  server_pothole_id,server_duplicate,central_sync_eligible,
                  detection_provider,tender_resolution_checked_at,
                  road_ownership,road_ownership_detail
                FROM reports WHERE id = 7
                """.trimIndent(),
            ).use { cursor ->
                assertTrue(cursor.moveToFirst())
                assertEquals(7L, cursor.getLong(cursor.getColumnIndexOrThrow("id")))
                assertEquals(
                    "pothole_cavity",
                    cursor.getString(cursor.getColumnIndexOrThrow("damage_type")),
                )
                assertEquals(12.9716, cursor.getDouble(cursor.getColumnIndexOrThrow("lat")), 0.0)
                assertEquals(77.5946, cursor.getDouble(cursor.getColumnIndexOrThrow("lng")), 0.0)
                assertEquals(3, cursor.getInt(cursor.getColumnIndexOrThrow("length(photo)")))
                assertEquals(3, cursor.getInt(cursor.getColumnIndexOrThrow("length(photo_full)")))
                assertTrue(cursor.isNull(cursor.getColumnIndexOrThrow("body_name")))
                assertTrue(cursor.isNull(cursor.getColumnIndexOrThrow("server_pothole_id")))
                assertEquals(0, cursor.getInt(cursor.getColumnIndexOrThrow("server_duplicate")))
                // Existing precise locations must not be uploaded merely because the app upgraded.
                assertEquals(0, cursor.getInt(cursor.getColumnIndexOrThrow("central_sync_eligible")))
                assertTrue(cursor.isNull(cursor.getColumnIndexOrThrow("detection_provider")))
                assertTrue(cursor.isNull(cursor.getColumnIndexOrThrow("tender_resolution_checked_at")))
                assertTrue(cursor.isNull(cursor.getColumnIndexOrThrow("road_ownership")))
                assertTrue(cursor.isNull(cursor.getColumnIndexOrThrow("road_ownership_detail")))
                assertFalse(cursor.moveToNext())
            }

            assertRetiredColumnsAreAbsent(database)

            // The new durable outbox must be usable and must not retain location metadata
            // after its owning report is deleted.
            database.execSQL("PRAGMA foreign_keys = ON")
            database.execSQL(
                """
                INSERT INTO central_observation_outbox (
                  client_observation_id,report_id,observed_at_ms,lat,lng,damage_type,
                  image_hash,detector_provider,detector_model,image_detail,evidence_count,
                  attempt_count
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """.trimIndent(),
                arrayOf<Any>(
                    "legacy-observation", 7, 1_725_000_000_000L, 12.9716, 77.5946,
                    "pothole_cavity", "a".repeat(64), "personal_openai", "gpt-5-mini",
                    "high", 2, 0,
                ),
            )
            assertEquals(1L, scalarLong(database, "SELECT COUNT(*) FROM central_observation_outbox"))
            database.execSQL("DELETE FROM reports WHERE id = 7")
            assertEquals(0L, scalarLong(database, "SELECT COUNT(*) FROM central_observation_outbox"))
        }
    }

    @Test
    fun migrate2To3_retiresPendingConditionAndRepairData() {
        createVersionOneDatabase().use { database -> insertLegacyReport(database) }
        migrationHelper.runMigrationsAndValidate(
            TEST_DATABASE,
            2,
            true,
            AppDatabase.MIGRATION_1_2,
        ).use { database ->
            database.execSQL(
                """
                UPDATE reports SET
                  condition_status = 'fixed',
                  condition_updated_at = 1725000100,
                  condition_source = 'user_reported',
                  condition_client_event_id = 'old-condition-event',
                  condition_sync_pending = 1,
                  repair_description = 'old repair evidence',
                  repair_photo = ?,
                  repair_observed_at = 1725000100,
                  repair_source_event_key = 'old-repair-event',
                  repair_same_location_visible = 1,
                  repair_completed_repair_visible = 1,
                  repair_current_condition = 'repaired',
                  repair_assessment = 'clear',
                  repair_image_quality = 'usable',
                  repair_detection_model = 'gpt-5-mini',
                  repair_prompt_version = 'road-repair-v2',
                  repair_schema_version = 2
                WHERE id = 7
                """.trimIndent(),
                arrayOf<Any>(byteArrayOf(9, 8, 7)),
            )
        }

        migrationHelper.runMigrationsAndValidate(
            TEST_DATABASE,
            3,
            true,
            AppDatabase.MIGRATION_2_3,
        ).use { database ->
            database.query("SELECT * FROM reports WHERE id = 7").use { cursor ->
                assertTrue(cursor.moveToFirst())
                assertEquals(
                    "open",
                    cursor.getString(cursor.getColumnIndexOrThrow("condition_status")),
                )
                assertEquals(0, cursor.getInt(cursor.getColumnIndexOrThrow("condition_sync_pending")))
                listOf(
                    "condition_updated_at",
                    "condition_source",
                    "condition_client_event_id",
                    "repair_description",
                    "repair_photo",
                    "repair_observed_at",
                    "repair_source_event_key",
                    "repair_same_location_visible",
                    "repair_completed_repair_visible",
                    "repair_current_condition",
                    "repair_assessment",
                    "repair_image_quality",
                    "repair_detection_model",
                    "repair_prompt_version",
                    "repair_schema_version",
                ).forEach { column ->
                    assertTrue("$column should be cleared", cursor.isNull(cursor.getColumnIndexOrThrow(column)))
                }
            }
        }
    }

    @Test
    fun migrate3To4_preservesEveryLiveReportAndOutboxFieldAndDropsRetiredColumns() {
        migrationHelper.createDatabase(TEST_DATABASE, 3).use { database ->
            database.execSQL(
                """
                INSERT INTO reports (
                  id,reportable,on_drivable_surface,has_broken_edge_or_rim,
                  has_depth_or_surface_loss,decision,status,capture_source,created_at,
                  seen_count,server_duplicate,central_sync_eligible,condition_status,
                  condition_sync_pending,dedupe_eligible,schema_version,evidence_count
                ) VALUES (
                  7,1,1,1,1,'accept','queued','drive_live',1725000001,3,1,1,
                  'fixed',1,1,4,2
                )
                """.trimIndent(),
            )
            database.execSQL(
                """
                UPDATE reports SET
                  assessment = 'damaged',
                  image_quality = 'acceptable',
                  damage_type = 'pothole_cavity',
                  temporal_consistency = 'legacy-sequence',
                  size = 'large',
                  description = 'deep road cavity',
                  lat = 12.9716,
                  lng = 77.5946,
                  gps_accuracy = 4.5,
                  speed_mps = 6.25,
                  heading = 187.5,
                  address = 'Vasanth Nagar, Bengaluru',
                  photo = X'010203',
                  photo_full = X'04050607',
                  drive_id = 'drive-v3',
                  source_event_key = 'drive-v3:7',
                  source_event_keys = '["drive-v3:7","drive-v3:8"]',
                  captured_at = 1725000000,
                  source_offset_s = 1.5,
                  last_seen_at = 1725000002,
                  server_pothole_id = 91,
                  central_sync_error = 'retry-later',
                  condition_updated_at = 1725000100,
                  condition_source = 'user_reported',
                  condition_client_event_id = 'retired-condition-event',
                  repair_description = 'retired repair evidence',
                  repair_photo = X'090807',
                  repair_observed_at = 1725000100,
                  repair_source_event_key = 'retired-repair-event',
                  repair_same_location_visible = 1,
                  repair_completed_repair_visible = 1,
                  repair_current_condition = 'repaired',
                  repair_assessment = 'clear',
                  repair_image_quality = 'usable',
                  repair_detection_model = 'retired-model',
                  repair_prompt_version = 'retired-prompt',
                  repair_schema_version = 2,
                  sighting_drive_ids = '["drive-v3"]',
                  event_sightings = '[{"event":"drive-v3:7"}]',
                  detection_provider = 'shared_server',
                  detection_model = 'gpt-5-mini',
                  image_detail = 'original',
                  prompt_version = 'road-damage-v4',
                  email_subject = 'Road damage report',
                  email_body = 'Please repair this road damage.',
                  email_to = 'commissioner@example.gov.in',
                  officer_title = 'Commissioner',
                  body_lgd = '276600',
                  body_name = 'BBMP',
                  tender_number = 'BBMP/2025-26/OW/WORK_INDENT7739',
                  contractor = 'Road Works Ltd',
                  tender_note = 'Likely matching road tender',
                  tender_resolution_reason = 'road surface and locality match',
                  tender_resolution_checked_at = 1725000200,
                  unrouted_reason = 'preserved-test-value'
                WHERE id = 7
                """.trimIndent(),
            )
            database.execSQL(
                """
                INSERT INTO central_observation_outbox (
                  client_observation_id,report_id,observed_at_ms,lat,lng,
                  gps_accuracy_m,heading_deg,speed_mps,damage_type,size,image_hash,
                  detector_provider,detector_model,image_detail,evidence_count,drive_id,
                  local_match_report_id,local_match_kind,attempt_count,last_error
                ) VALUES (
                  'observation-v3',7,1725000000000,12.9716,77.5946,4.5,187.5,6.25,
                  'pothole_cavity','large',
                  'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                  'shared_server','gpt-5-mini','original',2,'drive-v3',6,
                  'same_physical_damage',2,'temporary-error'
                )
                """.trimIndent(),
            )
        }

        migrationHelper.runMigrationsAndValidate(
            TEST_DATABASE,
            4,
            true,
            AppDatabase.MIGRATION_3_4,
        ).use { database ->
            assertRetiredColumnsAreAbsent(database)
            assertEquals(
                setOf("reports", "drive_sessions", "central_observation_outbox", "room_master_table"),
                userTableNames(database),
            )

            database.query("SELECT * FROM reports WHERE id = 7").use { cursor ->
                assertTrue(cursor.moveToFirst())
                mapOf(
                    "assessment" to "damaged",
                    "image_quality" to "acceptable",
                    "damage_type" to "pothole_cavity",
                    "size" to "large",
                    "description" to "deep road cavity",
                    "decision" to "accept",
                    "status" to "queued",
                    "address" to "Vasanth Nagar, Bengaluru",
                    "drive_id" to "drive-v3",
                    "capture_source" to "drive_live",
                    "source_event_key" to "drive-v3:7",
                    "source_event_keys" to "[\"drive-v3:7\",\"drive-v3:8\"]",
                    "central_sync_error" to "retry-later",
                    "sighting_drive_ids" to "[\"drive-v3\"]",
                    "event_sightings" to "[{\"event\":\"drive-v3:7\"}]",
                    "detection_provider" to "shared_server",
                    "detection_model" to "gpt-5-mini",
                    "image_detail" to "original",
                    "prompt_version" to "road-damage-v4",
                    "email_subject" to "Road damage report",
                    "email_body" to "Please repair this road damage.",
                    "email_to" to "commissioner@example.gov.in",
                    "officer_title" to "Commissioner",
                    "body_lgd" to "276600",
                    "body_name" to "BBMP",
                    "tender_number" to "BBMP/2025-26/OW/WORK_INDENT7739",
                    "contractor" to "Road Works Ltd",
                    "tender_note" to "Likely matching road tender",
                    "tender_resolution_reason" to "road surface and locality match",
                    "unrouted_reason" to "preserved-test-value",
                ).forEach { (column, expected) ->
                    assertEquals(column, expected, cursor.getString(cursor.getColumnIndexOrThrow(column)))
                }
                mapOf(
                    "lat" to 12.9716,
                    "lng" to 77.5946,
                    "gps_accuracy" to 4.5,
                    "speed_mps" to 6.25,
                    "heading" to 187.5,
                    "captured_at" to 1725000000.0,
                    "source_offset_s" to 1.5,
                    "created_at" to 1725000001.0,
                    "last_seen_at" to 1725000002.0,
                    "tender_resolution_checked_at" to 1725000200.0,
                ).forEach { (column, expected) ->
                    assertEquals(column, expected, cursor.getDouble(cursor.getColumnIndexOrThrow(column)), 0.0)
                }
                mapOf(
                    "seen_count" to 3L,
                    "server_pothole_id" to 91L,
                    "server_duplicate" to 1L,
                    "central_sync_eligible" to 1L,
                    "dedupe_eligible" to 1L,
                    "schema_version" to 4L,
                    "evidence_count" to 2L,
                ).forEach { (column, expected) ->
                    assertEquals(column, expected, cursor.getLong(cursor.getColumnIndexOrThrow(column)))
                }
                assertTrue(
                    cursor.getBlob(cursor.getColumnIndexOrThrow("photo"))
                        .contentEquals(byteArrayOf(1, 2, 3)),
                )
                assertTrue(
                    cursor.getBlob(cursor.getColumnIndexOrThrow("photo_full"))
                        .contentEquals(byteArrayOf(4, 5, 6, 7)),
                )
                assertFalse(cursor.moveToNext())
            }

            database.query("SELECT * FROM central_observation_outbox").use { cursor ->
                assertTrue(cursor.moveToFirst())
                mapOf(
                    "client_observation_id" to "observation-v3",
                    "damage_type" to "pothole_cavity",
                    "size" to "large",
                    "image_hash" to "a".repeat(64),
                    "detector_provider" to "shared_server",
                    "detector_model" to "gpt-5-mini",
                    "image_detail" to "original",
                    "drive_id" to "drive-v3",
                    "local_match_kind" to "same_physical_damage",
                    "last_error" to "temporary-error",
                ).forEach { (column, expected) ->
                    assertEquals(column, expected, cursor.getString(cursor.getColumnIndexOrThrow(column)))
                }
                mapOf(
                    "report_id" to 7L,
                    "observed_at_ms" to 1725000000000L,
                    "evidence_count" to 2L,
                    "local_match_report_id" to 6L,
                    "attempt_count" to 2L,
                ).forEach { (column, expected) ->
                    assertEquals(column, expected, cursor.getLong(cursor.getColumnIndexOrThrow(column)))
                }
                mapOf(
                    "lat" to 12.9716,
                    "lng" to 77.5946,
                    "gps_accuracy_m" to 4.5,
                    "heading_deg" to 187.5,
                    "speed_mps" to 6.25,
                ).forEach { (column, expected) ->
                    assertEquals(column, expected, cursor.getDouble(cursor.getColumnIndexOrThrow(column)), 0.0)
                }
                assertFalse(cursor.moveToNext())
            }

            database.execSQL("PRAGMA foreign_keys = ON")
            database.query("PRAGMA foreign_key_check").use { cursor ->
                assertFalse("migrated outbox must have no broken foreign keys", cursor.moveToFirst())
            }
            database.execSQL("DELETE FROM reports WHERE id = 7")
            assertEquals(0L, scalarLong(database, "SELECT COUNT(*) FROM central_observation_outbox"))
        }
    }

    @Test
    fun migrate4To5_preservesOutboxAndAddsNullableDetectionReceipt() {
        migrationHelper.createDatabase(TEST_DATABASE, 4).use { database ->
            database.execSQL(
                """
                INSERT INTO reports (
                  id,decision,status,capture_source,created_at,seen_count,server_duplicate,
                  central_sync_eligible,dedupe_eligible,schema_version,evidence_count
                ) VALUES (7,'accept','draft','drive_live',1725000001,1,0,1,1,4,1)
                """.trimIndent(),
            )
            database.execSQL(
                """
                INSERT INTO central_observation_outbox (
                  client_observation_id,report_id,observed_at_ms,lat,lng,damage_type,
                  image_hash,detector_provider,detector_model,image_detail,evidence_count,
                  attempt_count
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """.trimIndent(),
                arrayOf<Any>(
                    "receipt-migration-observation", 7, 1_725_000_000_000L,
                    12.9716, 77.5946, "pothole_cavity", "b".repeat(64),
                    "shared_server", "gpt-5-mini", "high", 1, 0,
                ),
            )
        }

        migrationHelper.runMigrationsAndValidate(
            TEST_DATABASE,
            5,
            true,
            AppDatabase.MIGRATION_4_5,
        ).use { database ->
            database.query(
                """
                SELECT client_observation_id,image_hash,detection_receipt
                FROM central_observation_outbox
                """.trimIndent(),
            ).use { cursor ->
                assertTrue(cursor.moveToFirst())
                assertEquals(
                    "receipt-migration-observation",
                    cursor.getString(cursor.getColumnIndexOrThrow("client_observation_id")),
                )
                assertEquals(
                    "b".repeat(64),
                    cursor.getString(cursor.getColumnIndexOrThrow("image_hash")),
                )
                assertTrue(cursor.isNull(cursor.getColumnIndexOrThrow("detection_receipt")))
                assertFalse(cursor.moveToNext())
            }
        }
    }

    @Test
    fun migrate5To6_addsOwnershipProofAndInvalidatesAllLegacyAcceptedCivicCaches() {
        migrationHelper.createDatabase(TEST_DATABASE, 5).use { database ->
            database.execSQL(
                """
                INSERT INTO reports (
                  id,decision,status,capture_source,created_at,seen_count,server_duplicate,
                  central_sync_eligible,dedupe_eligible,schema_version,evidence_count,
                  detection_provider,body_lgd,body_name,email_subject,email_body,email_to,
                  officer_title,tender_number,contractor,tender_note,
                  tender_resolution_reason,tender_resolution_checked_at
                ) VALUES (
                  7,'accept','queued','drive_live',1725000001,1,0,1,1,4,1,
                  'shared_server','276600','BBMP','subject','body','commissioner@example.gov.in',
                  'Commissioner','TENDER-7','Road Works Ltd','old match',
                  'no_confident_match',1725000200
                )
                """.trimIndent(),
            )
            database.execSQL(
                """
                INSERT INTO reports (
                  id,decision,status,capture_source,created_at,seen_count,server_duplicate,
                  central_sync_eligible,dedupe_eligible,schema_version,evidence_count,
                  detection_provider,body_lgd,body_name,email_to,
                  tender_resolution_reason,tender_resolution_checked_at
                ) VALUES (
                  8,'accept','duplicate','drive_live',1725000002,1,1,1,1,4,1,
                  'shared_server','276600','BBMP','commissioner@example.gov.in',
                  'state_highway',1725000201
                )
                """.trimIndent(),
            )
            database.execSQL(
                """
                INSERT INTO reports (
                  id,decision,status,capture_source,created_at,seen_count,server_duplicate,
                  central_sync_eligible,dedupe_eligible,schema_version,evidence_count,
                  detection_provider,body_lgd,body_name,email_to,
                  tender_resolution_reason,tender_resolution_checked_at
                ) VALUES (
                  9,'accept','queued','manual',1725000003,1,0,1,1,4,1,
                  'personal_openai','276600','BBMP','commissioner@example.gov.in',
                  'no_confident_match',1725000202
                )
                """.trimIndent(),
            )
        }

        migrationHelper.runMigrationsAndValidate(
            TEST_DATABASE,
            6,
            true,
            AppDatabase.MIGRATION_5_6,
        ).use { database ->
            database.query("SELECT * FROM reports WHERE id = 7").use { cursor ->
                assertTrue(cursor.moveToFirst())
                assertEquals("queued", cursor.getString(cursor.getColumnIndexOrThrow("status")))
                assertTrue(cursor.isNull(cursor.getColumnIndexOrThrow("unrouted_reason")))
                listOf(
                    "road_ownership",
                    "road_ownership_detail",
                    "tender_resolution_reason",
                    "tender_resolution_checked_at",
                    "body_lgd",
                    "body_name",
                    "email_subject",
                    "email_body",
                    "email_to",
                    "officer_title",
                    "tender_number",
                    "contractor",
                    "tender_note",
                ).forEach { column ->
                    assertTrue("$column must be invalidated", cursor.isNull(cursor.getColumnIndexOrThrow(column)))
                }
                assertFalse(cursor.moveToNext())
            }

            database.query("SELECT * FROM reports WHERE id = 8").use { cursor ->
                assertTrue(cursor.moveToFirst())
                assertEquals("duplicate", cursor.getString(cursor.getColumnIndexOrThrow("status")))
                assertTrue(cursor.isNull(cursor.getColumnIndexOrThrow("unrouted_reason")))
                assertTrue(cursor.isNull(cursor.getColumnIndexOrThrow("road_ownership")))
                assertTrue(cursor.isNull(cursor.getColumnIndexOrThrow("email_to")))
            }

            database.query("SELECT * FROM reports WHERE id = 9").use { cursor ->
                assertTrue(cursor.moveToFirst())
                assertEquals("queued", cursor.getString(cursor.getColumnIndexOrThrow("status")))
                listOf(
                    "body_lgd", "body_name", "email_to", "tender_resolution_reason",
                    "tender_resolution_checked_at", "road_ownership", "road_ownership_detail",
                ).forEach { column ->
                    assertTrue("$column must be invalidated", cursor.isNull(cursor.getColumnIndexOrThrow(column)))
                }
            }
        }
    }

    private fun insertLegacyReport(database: SupportSQLiteDatabase) {
        database.execSQL(
            """
            INSERT INTO reports (
              id,reportable,assessment,image_quality,damage_type,
              on_drivable_surface,has_broken_edge_or_rim,has_depth_or_surface_loss,
              decision,status,lat,lng,photo,photo_full,drive_id,capture_source,
              source_event_key,captured_at,created_at,seen_count,dedupe_eligible,
              detection_model,image_detail,schema_version,evidence_count,body_name
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """.trimIndent(),
            arrayOf<Any>(
                7, 1, "clear", "usable", "pothole_cavity",
                1, 1, 1,
                "accept", "draft", 12.9716, 77.5946,
                byteArrayOf(1, 2, 3), byteArrayOf(4, 5, 6),
                "legacy-drive", "drive_live", "drive:legacy:1",
                1_725_000_000.0, 1_725_000_001.0, 3, 1,
                "gpt-5-mini", "high", 3, 2, "Legacy civic body",
            ),
        )
    }

    private fun createVersionOneDatabase(): SupportSQLiteDatabase {
        context.deleteDatabase(TEST_DATABASE)
        val configuration = SupportSQLiteOpenHelper.Configuration.builder(context)
            .name(TEST_DATABASE)
            .callback(object : SupportSQLiteOpenHelper.Callback(1) {
                override fun onCreate(database: SupportSQLiteDatabase) {
                    database.execSQL(CREATE_REPORTS_V1)
                    database.execSQL(
                        "CREATE INDEX IF NOT EXISTS `index_reports_lat` ON `reports` (`lat`)",
                    )
                    database.execSQL(
                        "CREATE INDEX IF NOT EXISTS `index_reports_drive_id` ON `reports` (`drive_id`)",
                    )
                    database.execSQL(CREATE_DRIVE_SESSIONS_V1)
                }

                override fun onUpgrade(
                    database: SupportSQLiteDatabase,
                    oldVersion: Int,
                    newVersion: Int,
                ) = error("The test fixture must only create version 1")
            })
            .build()
        return FrameworkSQLiteOpenHelperFactory().create(configuration).writableDatabase
    }

    private fun scalarLong(database: SupportSQLiteDatabase, query: String): Long =
        database.query(query).use { cursor ->
            assertTrue(cursor.moveToFirst())
            cursor.getLong(0)
        }

    private fun assertRetiredColumnsAreAbsent(database: SupportSQLiteDatabase) {
        val columns = database.query("PRAGMA table_info(reports)").use { cursor ->
            buildSet {
                val nameIndex = cursor.getColumnIndexOrThrow("name")
                while (cursor.moveToNext()) add(cursor.getString(nameIndex))
            }
        }
        listOf(
            "reportable",
            "on_drivable_surface",
            "has_broken_edge_or_rim",
            "has_depth_or_surface_loss",
            "temporal_consistency",
            "condition_status",
            "condition_updated_at",
            "condition_source",
            "condition_client_event_id",
            "condition_sync_pending",
            "repair_description",
            "repair_photo",
            "repair_observed_at",
            "repair_source_event_key",
            "repair_same_location_visible",
            "repair_completed_repair_visible",
            "repair_current_condition",
            "repair_assessment",
            "repair_image_quality",
            "repair_detection_model",
            "repair_prompt_version",
            "repair_schema_version",
        ).forEach { column ->
            assertFalse("$column must not exist in schema v4", columns.contains(column))
        }
    }

    private fun userTableNames(database: SupportSQLiteDatabase): Set<String> =
        database.query(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
              AND name != 'android_metadata'
            """.trimIndent(),
        ).use { cursor ->
            buildSet {
                while (cursor.moveToNext()) add(cursor.getString(0))
            }
        }

    companion object {
        private const val TEST_DATABASE = "migration-test"

        // Exact Room v1 tables: v2 only adds central-service columns and the outbox.
        private val CREATE_REPORTS_V1 = """
            CREATE TABLE IF NOT EXISTS `reports` (
              `id` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
              `reportable` INTEGER NOT NULL,
              `assessment` TEXT,
              `image_quality` TEXT,
              `damage_type` TEXT,
              `on_drivable_surface` INTEGER NOT NULL,
              `has_broken_edge_or_rim` INTEGER NOT NULL,
              `has_depth_or_surface_loss` INTEGER NOT NULL,
              `temporal_consistency` TEXT,
              `size` TEXT,
              `description` TEXT,
              `decision` TEXT NOT NULL,
              `status` TEXT NOT NULL,
              `lat` REAL,
              `lng` REAL,
              `gps_accuracy` REAL,
              `speed_mps` REAL,
              `heading` REAL,
              `address` TEXT,
              `photo` BLOB,
              `photo_full` BLOB,
              `drive_id` TEXT,
              `capture_source` TEXT NOT NULL,
              `source_event_key` TEXT,
              `source_event_keys` TEXT,
              `captured_at` REAL,
              `source_offset_s` REAL,
              `created_at` REAL NOT NULL,
              `last_seen_at` REAL,
              `seen_count` INTEGER NOT NULL,
              `dedupe_eligible` INTEGER NOT NULL,
              `sighting_drive_ids` TEXT,
              `event_sightings` TEXT,
              `detection_model` TEXT,
              `image_detail` TEXT,
              `prompt_version` TEXT,
              `schema_version` INTEGER NOT NULL,
              `evidence_count` INTEGER NOT NULL,
              `email_subject` TEXT,
              `email_body` TEXT,
              `email_to` TEXT,
              `officer_title` TEXT,
              `body_name` TEXT,
              `unrouted_reason` TEXT
            )
        """.trimIndent()

        private val CREATE_DRIVE_SESSIONS_V1 = """
            CREATE TABLE IF NOT EXISTS `drive_sessions` (
              `id` TEXT NOT NULL,
              `started_at` REAL,
              `ended_at` REAL,
              `checked` INTEGER NOT NULL,
              `found` INTEGER NOT NULL,
              `already` INTEGER NOT NULL,
              `already_ids` TEXT,
              `gps_track` TEXT,
              PRIMARY KEY(`id`)
            )
        """.trimIndent()
    }
}
