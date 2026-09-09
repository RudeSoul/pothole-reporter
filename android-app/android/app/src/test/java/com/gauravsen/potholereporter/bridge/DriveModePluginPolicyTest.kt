package com.gauravsen.potholereporter.bridge

import android.Manifest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class DriveModePluginPolicyTest {

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
