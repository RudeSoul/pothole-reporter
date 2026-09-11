package com.gauravsen.potholereporter.bridge

import android.content.Intent
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class VideoImportPolicyTest {
    @Test
    fun acceptsColdAndWarmAndroidShareActionsOnly() {
        assertTrue(isVideoIngressAction(Intent.ACTION_SEND))
        assertTrue(isVideoIngressAction(Intent.ACTION_SEND_MULTIPLE))
        assertFalse(isVideoIngressAction(Intent.ACTION_VIEW))
        assertFalse(isVideoIngressAction(Intent.ACTION_MAIN))
        assertFalse(isVideoIngressAction(null))
    }

    @Test
    fun recognisesCommonMetaAndDashcamContainersWithoutTrustingArbitraryExtensions() {
        assertTrue(isPlausibleVideo("video/mp4", "meta-video.mp4"))
        assertTrue(isPlausibleVideo("video/quicktime", "clip.mov"))
        assertTrue(isPlausibleVideo("application/octet-stream", "dashcam.MKV"))
        assertTrue(isPlausibleVideo(null, "camera.M2TS"))

        assertFalse(isPlausibleVideo("image/jpeg", "renamed.mp4"))
        assertFalse(isPlausibleVideo("application/octet-stream", "payload.exe"))
        assertFalse(isPlausibleVideo(null, "no-extension"))
    }

    @Test
    fun externalShareCopyHasIndependentItemPendingAndFreeSpaceCaps() {
        assertNull(
            videoImportLimitFailure(
                declaredBytes = VIDEO_IMPORT_MAX_ITEM_BYTES,
                pendingBytes = 0,
                availableBytes = VIDEO_IMPORT_MAX_ITEM_BYTES + VIDEO_IMPORT_FREE_SPACE_RESERVE_BYTES,
            ),
        )
        assertEquals(
            VideoImportLimitFailure.ITEM_TOO_LARGE,
            videoImportLimitFailure(
                declaredBytes = VIDEO_IMPORT_MAX_ITEM_BYTES + 1,
                pendingBytes = 0,
                availableBytes = Long.MAX_VALUE,
            ),
        )
        assertEquals(
            VideoImportLimitFailure.PENDING_STORAGE_LIMIT,
            videoImportLimitFailure(
                declaredBytes = 1,
                pendingBytes = VIDEO_IMPORT_MAX_PENDING_BYTES,
                availableBytes = Long.MAX_VALUE,
            ),
        )
        assertEquals(
            VideoImportLimitFailure.LOW_DEVICE_STORAGE,
            videoImportLimitFailure(
                declaredBytes = 1,
                pendingBytes = 0,
                availableBytes = VIDEO_IMPORT_FREE_SPACE_RESERVE_BYTES,
            ),
        )
        assertEquals(
            12L,
            videoImportCopyBudget(
                pendingBytes = VIDEO_IMPORT_MAX_PENDING_BYTES - 12,
                availableBytes = Long.MAX_VALUE,
            ),
        )
    }

    @Test
    fun parsesOnlyUnambiguousBoundedIso6709Coordinates() {
        assertEquals(
            EmbeddedVideoLocation(12.9716, 77.5946),
            parseDecimalIso6709("+12.9716+077.5946/"),
        )
        assertEquals(
            EmbeddedVideoLocation(-33.8688, 151.2093),
            parseDecimalIso6709("-33.8688+151.2093"),
        )
        assertNull(parseDecimalIso6709("+91.0+077.0/"))
        assertNull(parseDecimalIso6709("+12.0+181.0/"))
        assertNull(parseDecimalIso6709("12.0,77.0"))
        assertNull(parseDecimalIso6709("+1234.00+07735.00/"))

        assertEquals(0L, parseVideoRecordedAtMs("19700101T000000Z"))
        assertEquals(0L, parseVideoRecordedAtMs("1970-01-01T05:30:00+05:30"))
        assertEquals(123L, parseVideoRecordedAtMs("19700101T000000.123456Z"))
        assertNull(parseVideoRecordedAtMs("20260909T232024")) // timezone must never be guessed
        assertNull(parseVideoRecordedAtMs("2026-13-09T23:20:24Z"))
        assertNull(parseVideoRecordedAtMs("2026-09-09T23:20:24+24:00"))
        assertNull(parseVideoRecordedAtMs("2026-09-09T23:20:24+05:60"))
    }

    @Test
    fun sanitisesProviderNamesAndBoundsFramePayloads() {
        assertEquals("trip 01.mp4", safeVideoDisplayName("../trip\n01.mp4"))
        assertEquals("mp4", videoFileExtension("TRIP.MP4"))
        assertEquals(240, clampedFrameHeight(10, 4000))
        assertEquals(480, clampedFrameHeight(1080, 480))
        assertEquals(1080, clampedFrameHeight(5000, null))
        assertEquals(55, clampedJpegQuality(1))
        assertEquals(92, clampedJpegQuality(100))
        assertEquals(
            BoundedFrameSize(1280, 720),
            boundedFrameSize(720, 1920, 1080, 0),
        )
        val hostile = boundedFrameSize(1080, Int.MAX_VALUE, 1, 0)
        assertTrue(hostile.width <= MAX_FRAME_DIMENSION)
        assertTrue(hostile.height <= MAX_FRAME_DIMENSION)
        assertTrue(hostile.width.toLong() * hostile.height <= MAX_FRAME_PIXELS)
    }

    @Test
    fun temporaryShareCopiesExpireButPersistedPickerUrisAreManagedExplicitly() {
        assertEquals(64, VIDEO_IMPORT_MAX_PICKER_ITEMS)
        assertEquals(4, VIDEO_IMPORT_MAX_BATCH_ITEMS)
        assertEquals(8, VIDEO_IMPORT_MAX_SHARED_PENDING_ITEMS)
        assertFalse(isExpiredVideoImport(1_000, 1_000 + VIDEO_IMPORT_RETENTION_MS - 1))
        assertTrue(isExpiredVideoImport(1_000, 1_000 + VIDEO_IMPORT_RETENTION_MS))
        assertFalse(
            isExpiredPersistedVideoImport(1_000, 1_000 + VIDEO_IMPORT_PERSISTED_RETENTION_MS - 1),
        )
        assertTrue(
            isExpiredPersistedVideoImport(1_000, 1_000 + VIDEO_IMPORT_PERSISTED_RETENTION_MS),
        )
        assertEquals("direct", videoPlaybackSupport("video/avc"))
        assertEquals("device_dependent", videoPlaybackSupport("video/hevc"))
        assertEquals("may_require_conversion", videoPlaybackSupport("video/mjpeg"))
    }
}
