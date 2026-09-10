package com.gauravsen.potholereporter.drivemode

import com.gauravsen.potholereporter.db.entities.ReportEntity
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class UploadWorkerPolicyTest {
    private fun staleMunicipalReport(
        status: String = "draft",
        serverDuplicate: Boolean = false,
    ) = ReportEntity(
        id = 17,
        decision = "accept",
        status = status,
        source_event_key = "drive:old:1",
        sourceEventKeys = "[\"drive:old:1\",\"drive:new:2\"]",
        server_duplicate = serverDuplicate,
        detection_provider = "shared_server",
        body_lgd = "999001",
        body_name = "Stale City Corporation",
        road_ownership = "municipal",
        road_ownership_detail = "Stale ownership detail",
        email_to = "stale@example.gov.in",
        officer_title = "Commissioner, Stale City Corporation",
        email_subject = "Old municipal complaint",
        email_body = "Old municipal body",
        tender_number = "OLD-TENDER",
        contractor = "Old Contractor",
        tender_note = "Old tender note",
        tender_resolution_reason = "old_match",
        tender_resolution_checked_at = 1.0,
        unrouted_reason = "outside_area",
    )

    private fun resolution(
        ownership: String,
        reason: String? = ownership.takeUnless { it == "municipal" },
    ) = CentralServiceClient.TenderResolution(
        address = "Current Road",
        bodyLgd = if (ownership == "municipal") "251234" else null,
        bodyName = if (ownership == "municipal") "Current City Corporation" else null,
        roadOwnership = ownership,
        ownershipDetail = when (ownership) {
            "rural" -> "Current Gram Panchayat"
            "outside_state" -> null
            "municipal" -> null
            else -> "Road 42"
        },
        tenderNumber = if (ownership == "municipal") "NEW-TENDER" else null,
        contractor = if (ownership == "municipal") "New Contractor" else null,
        tenderNote = if (ownership == "municipal") "New tender note" else null,
        reason = reason,
        requestId = "request-1",
    )

    @Test
    fun terminalOwnershipClearsEveryMunicipalAndSendableField() {
        val cases = mapOf(
            "national_highway" to "national_highway",
            "state_highway" to "state_highway",
            "district_highway" to "district_highway",
            "rural" to "rural_road",
            "outside_state" to "outside_area",
        )

        for ((ownership, expectedReason) in cases) {
            val updated = reportAfterTenderResolution(
                staleMunicipalReport(),
                resolution(ownership),
                checkedAt = 42.0,
            )

            assertEquals(ownership, "unrouted", updated.status)
            assertEquals(ownership, expectedReason, updated.unrouted_reason)
            assertEquals(ownership, ownership, updated.tender_resolution_reason)
            assertEquals(ownership, 42.0, updated.tender_resolution_checked_at)
            assertEquals(ownership, "Current Road", updated.address)
            assertNull(ownership, updated.body_lgd)
            assertNull(ownership, updated.body_name)
            assertEquals(ownership, ownership, updated.road_ownership)
            assertEquals(ownership, resolution(ownership).ownershipDetail,
                updated.road_ownership_detail)
            assertNull(ownership, updated.email_to)
            assertNull(ownership, updated.officer_title)
            assertNull(ownership, updated.email_subject)
            assertNull(ownership, updated.email_body)
            assertNull(ownership, updated.tender_number)
            assertNull(ownership, updated.contractor)
            assertNull(ownership, updated.tender_note)
        }
    }

    @Test
    fun municipalResolutionClearsStaleUnroutedStateAndCachedRecipient() {
        val updated = reportAfterTenderResolution(
            staleMunicipalReport(status = "unrouted"),
            resolution("municipal", reason = "no_confident_match"),
            checkedAt = 43.0,
        )

        assertEquals("queued", updated.status)
        assertNull(updated.unrouted_reason)
        assertEquals("251234", updated.body_lgd)
        assertEquals("Current City Corporation", updated.body_name)
        assertEquals("municipal", updated.road_ownership)
        assertNull(updated.road_ownership_detail)
        assertEquals("NEW-TENDER", updated.tender_number)
        assertEquals("New Contractor", updated.contractor)
        assertNull(updated.email_to)
        assertNull(updated.officer_title)
        assertNull(updated.email_subject)
        assertNull(updated.email_body)
    }

    @Test
    fun retryableOwnershipFailureFailsClosedAndRemainsUnchecked() {
        val updated = reportAfterTenderResolutionFailure(
            staleMunicipalReport(status = "queued"),
            reason = "road_ownership_unavailable",
            checkedAt = null,
        )

        assertEquals("unrouted", updated.status)
        assertEquals("road_class_unknown", updated.unrouted_reason)
        assertEquals("road_ownership_unavailable", updated.tender_resolution_reason)
        assertNull(updated.tender_resolution_checked_at)
        assertNull(updated.body_lgd)
        assertNull(updated.body_name)
        assertNull(updated.road_ownership)
        assertNull(updated.road_ownership_detail)
        assertNull(updated.email_to)
        assertNull(updated.email_subject)
        assertNull(updated.tender_number)
    }

    @Test
    fun duplicateRowsRemainDuplicateEvenWhenOwnershipIsTerminal() {
        val updated = reportAfterTenderResolution(
            staleMunicipalReport(status = "duplicate", serverDuplicate = true),
            resolution("state_highway"),
            checkedAt = 44.0,
        )

        assertEquals("duplicate", updated.status)
        assertEquals("state_highway", updated.unrouted_reason)
        assertNull(updated.body_lgd)
        assertNull(updated.email_to)
    }

    @Test
    fun sourceEventKeySuppliesTenderRetryOperationId() {
        assertEquals(
            "drive:new:2",
            tenderOperationId(staleMunicipalReport().copy(source_event_key = "drive:new:2")),
        )
        assertEquals(
            "drive:fallback:3",
            tenderOperationId(staleMunicipalReport().copy(
                source_event_key = "drive:fallback:3",
            )),
        )
        assertEquals(
            "native-report:17",
            tenderOperationId(staleMunicipalReport().copy(
                source_event_key = null,
                sourceEventKeys = null,
            )),
        )
    }

    @Test
    fun legacySharedRowWithoutOwnershipProofMustRevalidate() {
        val legacy = staleMunicipalReport().copy(road_ownership = null)

        assertEquals(true, reportNeedsOwnershipResolution(legacy))
        assertEquals(false, reportNeedsOwnershipResolution(
            legacy.copy(road_ownership = "municipal"),
        ))
        assertEquals(false, reportNeedsOwnershipResolution(
            legacy.copy(detection_provider = "personal_openai"),
        ))
        assertEquals(true, reportNeedsOwnershipResolution(
            legacy.copy(
                detection_provider = "personal_openai",
                tender_resolution_checked_at = null,
            ),
        ))
    }
}
