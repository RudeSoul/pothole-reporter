package com.gauravsen.potholereporter.drivemode

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DriveSessionDrainTest {
    @Test
    fun detectionBacklogIsBoundedAndVisibleInStatus() {
        val session = DriveSession("bounded")

        assertTrue(session.tryBeginDetection(3))
        assertTrue(session.tryBeginDetection(3))
        assertTrue(session.tryBeginDetection(3))
        assertFalse(session.tryBeginDetection(3))

        assertEquals(3, session.inFlight)
        assertEquals(1, session.dropped)
        val tally = session.snapshot()["tally"] as Map<*, *>
        assertEquals(3, tally["analyzing"])
    }

    @Test
    fun stopFinalizationCanBeClaimedOnceAndOnlyAfterDrain() {
        val session = DriveSession("drain")
        session.stopping = true
        session.state = DriveSession.State.STOPPING
        assertTrue(session.tryBeginDetection(2))
        assertTrue(session.tryBeginDetection(2))

        assertFalse(session.tryBeginStopFinalization())
        assertEquals(1, session.finishDetection())
        assertFalse(session.tryBeginStopFinalization())
        assertEquals(0, session.finishDetection())
        assertTrue(session.tryBeginStopFinalization())
        assertFalse(session.tryBeginStopFinalization())
    }

    @Test
    fun expectedCameraCloseDuringStopDoesNotCancelTheDrainTick() {
        assertTrue(shouldTreatCameraClosedAsConflict(stopping = false))
        assertFalse(shouldTreatCameraClosedAsConflict(stopping = true))
    }

    @Test
    fun stoppingBeforeTheRequestedFrameClearsThePendingCapture() {
        val analyzer = FrameAnalyzer { throw AssertionError("a cancelled frame must not be delivered") }
        analyzer.requestCapture()

        assertTrue(analyzer.isCapturing())
        assertTrue(analyzer.cancelPendingCapture())
        assertFalse(analyzer.isCapturing())
        assertFalse(analyzer.cancelPendingCapture())
    }
}
