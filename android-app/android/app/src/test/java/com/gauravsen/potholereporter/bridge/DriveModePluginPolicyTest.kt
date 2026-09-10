package com.gauravsen.potholereporter.bridge

import android.Manifest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DriveModePluginPolicyTest {

    private fun maySave(
        decision: String = "accept",
        status: String = "queued",
        duplicate: Boolean = false,
        ownership: String? = null,
        checkedAt: Double? = null,
        tenderNumber: String? = null,
        serverPotholeId: Long? = 99L,
        contractor: String? = null,
        expectedContractor: String? = contractor,
        unroutedReason: String? = null,
        pendingCentral: Boolean = false,
        expectedOwnership: String? = ownership,
        expectedCheckedAt: Double? = checkedAt,
        expectedTenderNumber: String? = tenderNumber,
        expectedServerPotholeId: Long? = serverPotholeId,
    ) = canSaveComplaintPreparation(
        decision = decision,
        status = status,
        serverDuplicate = duplicate,
        unroutedReason = unroutedReason,
        hasPendingCentralObservation = pendingCentral,
        current = ComplaintCivicSnapshot(
            roadOwnership = ownership,
            tenderResolutionCheckedAt = checkedAt,
            tenderNumber = tenderNumber,
            serverPotholeId = serverPotholeId,
            address = "Road", bodyLgd = "1", bodyName = "Town",
            emailTo = "officer@example.gov.in", officerTitle = "Commissioner",
            contractor = contractor, tenderNote = null,
        ),
        expected = ComplaintCivicSnapshot(
            roadOwnership = expectedOwnership,
            tenderResolutionCheckedAt = expectedCheckedAt,
            tenderNumber = expectedTenderNumber,
            serverPotholeId = expectedServerPotholeId,
            address = "Road", bodyLgd = "1", bodyName = "Town",
            emailTo = "officer@example.gov.in", officerTitle = "Commissioner",
            contractor = expectedContractor, tenderNote = null,
        ),
    )

    @Test
    fun notificationPermissionDoesNotBlockForegroundDriveMode() {
        val required = requiredDriveModeRuntimePermissions()

        assertEquals(
            listOf(Manifest.permission.CAMERA, Manifest.permission.ACCESS_FINE_LOCATION),
            required,
        )
        assertFalse(required.contains(Manifest.permission.POST_NOTIFICATIONS))
    }

    @Test
    fun complaintPreparationAllowsOnlyAnUnchangedSendableAuthoritySnapshot() {
        assertTrue(maySave()) // first central resolution on a clean draft
        assertTrue(maySave(ownership = "municipal", checkedAt = 123.0, tenderNumber = "T-1"))

        assertFalse(maySave(unroutedReason = "road_class_unknown"))
        assertFalse(maySave(duplicate = true))
        assertFalse(maySave(serverPotholeId = null))
        assertFalse(maySave(pendingCentral = true))
        assertFalse(maySave(status = "unrouted"))
        assertFalse(maySave(decision = "reject"))
        assertFalse(maySave(ownership = "national_highway"))
        assertFalse(maySave(
            ownership = "municipal",
            expectedOwnership = null,
            checkedAt = 123.0,
            expectedCheckedAt = null,
        ))
        assertFalse(maySave(
            ownership = "municipal",
            checkedAt = 124.0,
            expectedCheckedAt = 123.0,
        ))
        assertFalse(maySave(
            ownership = "municipal",
            checkedAt = 123.0,
            tenderNumber = "T-2",
            expectedTenderNumber = "T-1",
        ))
        assertFalse(maySave(
            ownership = "municipal",
            checkedAt = 123.0,
            tenderNumber = "T-1",
            contractor = "New contractor",
            expectedContractor = "Old contractor",
        ))
    }

    @Test
    fun startCompletesOnlyForARealNonEmptySessionId() {
        assertEquals(
            DriveModePollDecision.WAIT,
            driveModeStartPollDecision(null, nowMs = 10, deadlineMs = 20),
        )
        assertEquals(
            DriveModePollDecision.WAIT,
            driveModeStartPollDecision("", nowMs = 10, deadlineMs = 20),
        )
        assertEquals(
            DriveModePollDecision.COMPLETE,
            driveModeStartPollDecision("session-123", nowMs = 10, deadlineMs = 20),
        )
        assertEquals(
            DriveModePollDecision.TIMED_OUT,
            driveModeStartPollDecision(null, nowMs = 20, deadlineMs = 20),
        )
    }

    @Test
    fun stopWaitsForServiceShutdownAndHasABoundedTimeout() {
        assertEquals(
            DriveModePollDecision.WAIT,
            driveModeStopPollDecision(isRunning = true, nowMs = 10, deadlineMs = 20),
        )
        assertEquals(
            DriveModePollDecision.COMPLETE,
            driveModeStopPollDecision(isRunning = false, nowMs = 10, deadlineMs = 20),
        )
        assertEquals(
            DriveModePollDecision.TIMED_OUT,
            driveModeStopPollDecision(isRunning = true, nowMs = 20, deadlineMs = 20),
        )
    }
}
