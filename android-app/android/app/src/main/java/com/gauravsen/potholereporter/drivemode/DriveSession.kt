package com.gauravsen.potholereporter.drivemode

import android.location.Location

/**
 * Tracks the state of a single Drive Mode session.
 *
 * Detection jobs can finish concurrently, so compound tally/backoff updates use
 * synchronized helpers. Volatile fields keep simple capture/GPS reads visible.
 */
class DriveSession(val sessionId: String) {

    val startedAtMs: Long = System.currentTimeMillis()

    // Tally — mirrors the web client's ctx.tally
    @Volatile var captured: Int = 0
    @Volatile var checked: Int = 0
    @Volatile var found: Int = 0
    @Volatile var already: Int = 0
    @Volatile var dropped: Int = 0
    @Volatile var failed: Int = 0
    @Volatile var errors: Int = 0
    @Volatile var consecutiveVisionFailures: Int = 0
    @Volatile var visionBackoffUntilMs: Long = 0L
    @Volatile var lastError: String? = null

    // Capture gating
    @Volatile var lastCapturePos: Location? = null
    @Volatile var lastCaptureAtMs: Long = 0L
    @Volatile var captureSeq: Int = 0
    @Volatile var stillBusy: Boolean = false
    @Volatile var camBadTicks: Int = 0
    @Volatile var capBadTicks: Int = 0

    // GPS state — updated by LocationTracker
    @Volatile var currentLocation: Location? = null
    @Volatile var locationFreshAtMs: Long = 0L

    // Session lifecycle
    @Volatile var state: State = State.STARTING
    @Volatile var cameraState: CameraState = CameraState.UNAVAILABLE
    @Volatile var stopping: Boolean = false

    // Duplicate tracking
    val duplicateIds: MutableSet<String> = mutableSetOf()

    // Every detection reserved by the capture loop, including work waiting for the
    // dispatcher's network semaphore. Counting the whole backlog is what lets Stop drain
    // deterministically and prevents a long drive from launching unbounded coroutines.
    @Volatile var inFlight: Int = 0
    @Volatile private var stopFinalizationStarted: Boolean = false

    val durationMs: Long get() = System.currentTimeMillis() - startedAtMs

    @Synchronized fun tryBeginDetection(maxOutstanding: Int): Boolean {
        if (inFlight >= maxOutstanding) {
            dropped++
            return false
        }
        inFlight++
        return true
    }

    @Synchronized fun finishDetection(): Int {
        inFlight = (inFlight - 1).coerceAtLeast(0)
        return inFlight
    }

    /** Claim the one finalization turn only after every reserved detection has finished. */
    @Synchronized fun tryBeginStopFinalization(): Boolean {
        if (!stopping || inFlight != 0 || stopFinalizationStarted) return false
        stopFinalizationStarted = true
        return true
    }

    @Synchronized fun recordChecked() {
        checked++
        consecutiveVisionFailures = 0
        visionBackoffUntilMs = 0L
        lastError = null
    }

    @Synchronized fun recordFinding(isDuplicate: Boolean) {
        if (isDuplicate) already++ else found++
    }

    @Synchronized fun recordError() { errors++ }

    @Synchronized fun recordVisionFailure(message: String): Long {
        failed++
        errors++
        consecutiveVisionFailures++
        val exponent = (consecutiveVisionFailures - 1).coerceIn(0, 5)
        val backoffMs = (15_000L * (1L shl exponent)).coerceAtMost(5 * 60_000L)
        visionBackoffUntilMs = System.currentTimeMillis() + backoffMs
        lastError = message.take(240)
        return backoffMs
    }

    enum class State {
        STARTING,
        RUNNING,
        PAUSED,
        STOPPING,
        STOPPED
    }

    enum class CameraState {
        ACTIVE,
        PAUSED_CONFLICT,
        UNAVAILABLE
    }

    /**
     * Thread-safe snapshot for the Capacitor bridge to read.
     */
    @Synchronized fun snapshot(): Map<String, Any?> = mapOf(
        "state" to state.name.lowercase(),
        "sessionId" to sessionId,
        "cameraState" to cameraState.name.lowercase(),
        "durationMs" to durationMs,
        "lastError" to lastError,
        "tally" to mapOf(
            "checked" to checked,
            "found" to found,
            "already" to already,
            "captured" to captured,
            "dropped" to dropped,
            "failed" to failed,
            "errors" to errors,
            "analyzing" to inFlight,
        )
    )

    /**
     * Whether the GPS fix is fresh enough to use for gating and tagging.
     */
    fun isGpsFresh(): Boolean {
        val loc = currentLocation ?: return false
        val fixAge = System.currentTimeMillis() - locationFreshAtMs
        return fixAge <= DriveConstants.GPS_MAX_AGE_MS
    }

    /**
     * Speed from the most recent location fix, or null if unavailable.
     */
    fun currentSpeed(): Float? {
        val loc = currentLocation ?: return null
        return if (loc.hasSpeed()) loc.speed else null
    }

    /**
     * Heading from the most recent location fix, or null if unavailable.
     */
    fun currentHeading(): Float? {
        val loc = currentLocation ?: return null
        return if (loc.hasBearing()) loc.bearing else null
    }

    /**
     * Accuracy from the most recent location fix, or null if unavailable.
     */
    fun currentAccuracy(): Float? {
        val loc = currentLocation ?: return null
        return if (loc.hasAccuracy()) loc.accuracy else null
    }
}
