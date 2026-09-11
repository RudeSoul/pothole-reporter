package com.gauravsen.potholereporter.bridge

import android.content.Context
import android.content.Intent
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMetadataRetriever
import android.net.Uri
import android.os.CancellationSignal
import android.os.ParcelFileDescriptor
import android.os.StatFs
import android.provider.OpenableColumns
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

internal data class SharedVideoSource(
    val uri: Uri,
    val intentMimeType: String?,
    val sourceAction: String,
)

internal data class PendingVideoImport(
    val id: String,
    val displayName: String,
    val mimeType: String,
    val bytes: Long,
    val createdAtMs: Long,
    val storageMode: String,
    val filePath: String?,
    val contentUri: String?,
    val sourceFingerprint: String?,
    val contentFingerprint: String?,
    val sourceAction: String,
    val durationMs: Long?,
    val recordedAt: String?,
    val recordedAtMs: Long?,
    val width: Int?,
    val height: Int?,
    val rotationDegrees: Int?,
    val codecMime: String?,
    val embeddedLocation: EmbeddedVideoLocation?,
) {
    fun persistedJson(): JSONObject = publicJson()
        .put("bytes", bytes)
        .put("file_path", filePath ?: JSONObject.NULL)
        .put("content_uri", contentUri ?: JSONObject.NULL)
        .put("source_fingerprint", sourceFingerprint ?: JSONObject.NULL)
        .put("content_fingerprint", contentFingerprint ?: JSONObject.NULL)

    fun publicJson(): JSONObject = JSONObject()
        .put("id", id)
        .put("display_name", displayName)
        .put("mime_type", mimeType)
        .put("bytes", bytes.takeIf { it >= 0L } ?: JSONObject.NULL)
        .put("created_at_ms", createdAtMs)
        // A short-clip HTML fallback may pass this through Capacitor.convertFileSrc().
        // Multi-GB analysis should call the native frame-extraction methods instead.
        .put("file_uri", accessUri().toString())
        .put("storage_mode", storageMode)
        .put("source", if (storageMode == STORAGE_PERSISTED_URI) "android_picker" else "android_share")
        .put("source_action", sourceAction)
        // Stable opaque identity for UI queue keys; the token itself contains no path or URI.
        .put("content_token", contentFingerprint ?: sourceFingerprint ?: JSONObject.NULL)
        .put("duration_ms", durationMs ?: JSONObject.NULL)
        .put("recorded_at_raw", recordedAt ?: JSONObject.NULL)
        .put("recorded_at_ms", recordedAtMs ?: JSONObject.NULL)
        .put("width", width ?: JSONObject.NULL)
        .put("height", height ?: JSONObject.NULL)
        .put("rotation_degrees", rotationDegrees ?: JSONObject.NULL)
        .put("codec_mime", codecMime ?: JSONObject.NULL)
        .put("playback_support", videoPlaybackSupport(codecMime))
        .put(
            "embedded_location",
            embeddedLocation?.let {
                JSONObject()
                    .put("lat", it.lat)
                    .put("lng", it.lng)
                    .put("source", "video_metadata_static")
                    .put("routing_eligible", false)
            } ?: JSONObject.NULL,
        )

    companion object {
        const val STORAGE_CACHE_COPY = "temporary_cache_copy"
        const val STORAGE_PERSISTED_URI = "persisted_content_uri"

        fun fromJson(json: JSONObject): PendingVideoImport? = runCatching {
            PendingVideoImport(
                id = json.getString("id"),
                displayName = json.getString("display_name"),
                mimeType = json.getString("mime_type"),
                bytes = json.getLong("bytes"),
                createdAtMs = json.getLong("created_at_ms"),
                storageMode = json.optString("storage_mode", STORAGE_CACHE_COPY),
                filePath = json.optBoundedString("file_path", 4096),
                contentUri = json.optBoundedString("content_uri", 4096),
                sourceFingerprint = json.optBoundedString("source_fingerprint", 64),
                contentFingerprint = json.optBoundedString("content_fingerprint", 64),
                sourceAction = json.optString("source_action", Intent.ACTION_SEND),
                durationMs = json.optLongOrNull("duration_ms"),
                recordedAt = json.optBoundedString("recorded_at_raw")
                    ?: json.optBoundedString("recorded_at"),
                recordedAtMs = json.optLongOrNull("recorded_at_ms"),
                width = json.optIntOrNull("width"),
                height = json.optIntOrNull("height"),
                rotationDegrees = json.optIntOrNull("rotation_degrees"),
                codecMime = json.optBoundedString("codec_mime"),
                embeddedLocation = json.optJSONObject("embedded_location")?.let {
                    val lat = it.optDouble("lat", Double.NaN)
                    val lng = it.optDouble("lng", Double.NaN)
                    if (lat.isFinite() && lng.isFinite() && lat in -90.0..90.0 && lng in -180.0..180.0) {
                        EmbeddedVideoLocation(lat, lng)
                    } else null
                },
            )
        }.getOrNull()
    }

    fun accessUri(): Uri = when (storageMode) {
        STORAGE_PERSISTED_URI -> Uri.parse(requireNotNull(contentUri))
        else -> Uri.fromFile(File(requireNotNull(filePath)))
    }
}

internal data class VideoImportError(
    val code: String,
    val message: String,
    val displayName: String?,
    val atMs: Long,
) {
    fun json(): JSONObject = JSONObject()
        .put("code", code)
        .put("message", message)
        .put("display_name", displayName ?: JSONObject.NULL)
        .put("at_ms", atMs)

    companion object {
        fun fromJson(json: JSONObject): VideoImportError? = runCatching {
            VideoImportError(
                code = json.getString("code").take(64),
                message = json.getString("message").take(512),
                displayName = json.optBoundedString("display_name", 120),
                atMs = json.getLong("at_ms"),
            )
        }.getOrNull()
    }
}

internal data class VideoImportSnapshot(
    val imports: List<PendingVideoImport>,
    val error: VideoImportError?,
) {
    fun json(): JSONObject = JSONObject()
        .put("imports", JSONArray().also { array -> imports.forEach { array.put(it.publicJson()) } })
        .put("pending_count", imports.size)
        .put(
            "pending_bytes",
            imports.filter { it.storageMode == PendingVideoImport.STORAGE_CACHE_COPY }.sumOf { it.bytes },
        )
        .put("error", error?.json() ?: JSONObject.NULL)
        .put("max_shared_import_bytes", VIDEO_IMPORT_MAX_ITEM_BYTES)
        .put("max_pending_bytes", VIDEO_IMPORT_MAX_PENDING_BYTES)
        .put("max_picker_files", VIDEO_IMPORT_MAX_PICKER_ITEMS)
        .put("max_shared_pending_files", VIDEO_IMPORT_MAX_SHARED_PENDING_ITEMS)
}

internal class VideoImportException(
    val code: String,
    override val message: String,
    val displayName: String? = null,
) : IOException(message)

/**
 * Stores temporary, private copies of videos delivered by external share intents.
 * All public methods are called under VideoImportPlugin's process-wide ingest mutex.
 */
internal class VideoImportStore(context: Context) {
    private val appContext = context.applicationContext
    private val prefs = appContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
    private val root = File(appContext.cacheDir, DIRECTORY_NAME)

    fun snapshot(nowMs: Long = System.currentTimeMillis()): VideoImportSnapshot {
        val imports = loadAndCleanup(nowMs)
        return VideoImportSnapshot(imports, loadError())
    }

    fun import(sources: List<SharedVideoSource>, nowMs: Long = System.currentTimeMillis()): VideoImportSnapshot {
        if (sources.isEmpty()) {
            return fail("missing_video", "No readable video was attached to that share.", null, nowMs)
        }
        if (sources.size > VIDEO_IMPORT_MAX_BATCH_ITEMS) {
            return fail(
                "too_many_videos",
                "Share at most $VIDEO_IMPORT_MAX_BATCH_ITEMS videos at once.",
                null,
                nowMs,
            )
        }

        root.mkdirs()
        if (!root.isDirectory) {
            return fail("storage_unavailable", "The app could not create temporary video storage.", null, nowMs)
        }

        val existing = loadAndCleanup(nowMs)
        val uniqueSources = sources.distinctBy { it.uri.toString() }
        if (uniqueSources.isEmpty()) {
            clearError()
            return VideoImportSnapshot(existing, null)
        }
        if (existing.size + uniqueSources.size > VIDEO_IMPORT_MAX_PICKER_ITEMS) {
            return fail(
                "too_many_pending_videos",
                "Finish or discard pending videos before sharing more (maximum $VIDEO_IMPORT_MAX_PICKER_ITEMS).",
                null,
                nowMs,
            )
        }
        val existingSharedCount = existing.count {
            it.storageMode == PendingVideoImport.STORAGE_CACHE_COPY
        }
        if (existingSharedCount + uniqueSources.size > VIDEO_IMPORT_MAX_SHARED_PENDING_ITEMS) {
            return fail(
                "too_many_shared_videos",
                "Finish or discard shared videos before sharing more " +
                    "(maximum $VIDEO_IMPORT_MAX_SHARED_PENDING_ITEMS pending shared clips).",
                null,
                nowMs,
            )
        }
        var pendingBytes = existing
            .filter { it.storageMode == PendingVideoImport.STORAGE_CACHE_COPY }
            .sumOf { it.bytes }
        val stagedFiles = mutableListOf<File>()
        val stagedImports = mutableListOf<PendingVideoImport>()
        val knownContentFingerprints = existing
            .mapNotNullTo(mutableSetOf()) { it.contentFingerprint }

        try {
            for (source in uniqueSources) {
                val info = inspectSource(source)
                if (!isPlausibleVideo(info.mimeType, info.displayName)) {
                    throw VideoImportException(
                        "not_a_video",
                        "${info.displayName} is not a supported video file.",
                        info.displayName,
                    )
                }

                val available = availableBytes()
                videoImportLimitFailure(info.declaredBytes, pendingBytes, available)?.let { failure ->
                    throw limitException(failure, info.displayName)
                }
                val copyBudget = videoImportCopyBudget(pendingBytes, available)
                val id = UUID.randomUUID().toString()
                val extension = videoFileExtension(info.displayName) ?: extensionForMime(info.mimeType)
                val partial = File(root, "$id.part")
                val destination = File(root, if (extension == null) id else "$id.$extension")
                stagedFiles += partial
                stagedFiles += destination

                val copy = copyBounded(source.uri, partial, copyBudget, info.displayName)
                val metadata = readMetadata(Uri.fromFile(partial), info.displayName, requireVideoTrack = true)
                // External providers can mint a fresh URI for the same clip, and can also
                // change bytes behind an old URI. Deduplicate the bytes actually copied,
                // not the provider-controlled URI string.
                if (!knownContentFingerprints.add(copy.sha256)) {
                    partial.delete()
                    continue
                }
                if (!partial.renameTo(destination)) {
                    throw VideoImportException(
                        "storage_failure",
                        "The shared video could not be finalised in app storage.",
                        info.displayName,
                    )
                }
                val imported = PendingVideoImport(
                    id = id,
                    displayName = info.displayName,
                    mimeType = info.mimeType,
                    bytes = copy.bytes,
                    createdAtMs = nowMs,
                    storageMode = PendingVideoImport.STORAGE_CACHE_COPY,
                    filePath = destination.absolutePath,
                    contentUri = null,
                    sourceFingerprint = sourceFingerprint(source.uri),
                    contentFingerprint = copy.sha256,
                    sourceAction = source.sourceAction,
                    durationMs = metadata.durationMs,
                    recordedAt = metadata.recordedAt,
                    recordedAtMs = metadata.recordedAtMs,
                    width = metadata.width,
                    height = metadata.height,
                    rotationDegrees = metadata.rotationDegrees,
                    codecMime = metadata.codecMime,
                    embeddedLocation = metadata.embeddedLocation,
                )
                stagedImports += imported
                pendingBytes += copy.bytes
            }

            val combined = existing + stagedImports
            if (!saveImports(combined)) {
                throw VideoImportException(
                    "storage_failure",
                    "The app could not persist the shared-video handoff.",
                )
            }
            clearError()
            // Only the final files are now owned by the persisted records.
            stagedFiles.filter { it.extension == "part" }.forEach(File::delete)
            return VideoImportSnapshot(combined, null)
        } catch (error: VideoImportException) {
            stagedFiles.forEach(File::delete)
            return fail(error.code, error.message, error.displayName, nowMs)
        } catch (_: SecurityException) {
            stagedFiles.forEach(File::delete)
            return fail(
                "permission_expired",
                "The sending app did not grant access to that video. Use Import video and select it from Files instead.",
                null,
                nowMs,
            )
        } catch (_: Exception) {
            stagedFiles.forEach(File::delete)
            return fail(
                "import_failed",
                "The shared video could not be imported. Use Import video and select it from Files instead.",
                null,
                nowMs,
            )
        }
    }

    fun registerPersistedUri(
        uri: Uri,
        intentMimeType: String?,
        sourceAction: String,
        grantFlags: Int,
        nowMs: Long = System.currentTimeMillis(),
    ): VideoImportSnapshot = registerPersistedUris(
        sources = listOf(SharedVideoSource(uri, intentMimeType, sourceAction)),
        grantFlags = grantFlags,
        nowMs = nowMs,
    )

    fun registerPersistedUris(
        sources: List<SharedVideoSource>,
        grantFlags: Int,
        nowMs: Long = System.currentTimeMillis(),
    ): VideoImportSnapshot {
        val existing = loadAndCleanup(nowMs)
        val existingUris = existing.mapNotNullTo(mutableSetOf()) { it.contentUri }
        val uniqueSources = sources.distinctBy { it.uri.toString() }
            .filterNot { it.uri.toString() in existingUris }
        if (sources.isEmpty()) {
            return fail("missing_video", "No video was selected.", null, nowMs)
        }
        if (uniqueSources.isEmpty()) {
            prefs.edit().remove(PREF_ERROR).commit()
            return VideoImportSnapshot(existing, null)
        }
        if (uniqueSources.size > VIDEO_IMPORT_MAX_PICKER_ITEMS ||
            existing.size + uniqueSources.size > VIDEO_IMPORT_MAX_PICKER_ITEMS
        ) {
            return fail(
                "too_many_pending_videos",
                "Select at most $VIDEO_IMPORT_MAX_PICKER_ITEMS clips, and finish or discard pending videos first.",
                null,
                nowMs,
            )
        }
        if (uniqueSources.any { it.uri.scheme != "content" }) {
            return fail("invalid_video_uri", "Select a video from the Android system picker.", null, nowMs)
        }

        val grantedUris = mutableListOf<Uri>()
        return try {
            val takeFlags = grantFlags and Intent.FLAG_GRANT_READ_URI_PERMISSION
            if (takeFlags == 0) {
                return fail(
                    "permission_not_persistable",
                    "The selected provider did not grant read access. Select the videos from Files instead.",
                    null,
                    nowMs,
                )
            }
            val imported = uniqueSources.map { source ->
                val info = inspectSource(source)
                if (!isPlausibleVideo(info.mimeType, info.displayName)) {
                    throw VideoImportException(
                        "not_a_video",
                        "${info.displayName} is not a supported video file.",
                        info.displayName,
                    )
                }
                appContext.contentResolver.takePersistableUriPermission(source.uri, takeFlags)
                grantedUris += source.uri
                val metadata = readMetadata(source.uri, info.displayName, requireVideoTrack = true)
                PendingVideoImport(
                    id = UUID.randomUUID().toString(),
                    displayName = info.displayName,
                    mimeType = info.mimeType,
                    bytes = info.declaredBytes ?: -1L,
                    createdAtMs = nowMs,
                    storageMode = PendingVideoImport.STORAGE_PERSISTED_URI,
                    filePath = null,
                    contentUri = source.uri.toString(),
                    sourceFingerprint = sourceFingerprint(source.uri),
                    contentFingerprint = null,
                    sourceAction = source.sourceAction,
                    durationMs = metadata.durationMs,
                    recordedAt = metadata.recordedAt,
                    recordedAtMs = metadata.recordedAtMs,
                    width = metadata.width,
                    height = metadata.height,
                    rotationDegrees = metadata.rotationDegrees,
                    codecMime = metadata.codecMime,
                    embeddedLocation = metadata.embeddedLocation,
                )
            }
            val combined = existing + imported
            if (!saveImports(combined)) {
                throw VideoImportException(
                    "storage_failure",
                    "The app could not persist access to the selected videos.",
                )
            }
            prefs.edit().remove(PREF_ERROR).commit()
            VideoImportSnapshot(combined, null)
        } catch (error: VideoImportException) {
            grantedUris.forEach { uri ->
                runCatching { appContext.contentResolver.releasePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
            }
            fail(error.code, error.message, error.displayName, nowMs)
        } catch (_: SecurityException) {
            grantedUris.forEach { uri ->
                runCatching { appContext.contentResolver.releasePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
            }
            fail(
                "permission_not_persistable",
                "The selected provider did not allow lasting access. Select the videos from Files instead.",
                null,
                nowMs,
            )
        } catch (_: Exception) {
            grantedUris.forEach { uri ->
                runCatching { appContext.contentResolver.releasePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
            }
            fail(
                "import_failed",
                "A selected video could not be opened. Try selecting the clips from Files again.",
                null,
                nowMs,
            )
        }
    }

    fun find(id: String, nowMs: Long = System.currentTimeMillis()): PendingVideoImport? =
        loadAndCleanup(nowMs).firstOrNull { it.id == id }

    fun reject(
        code: String,
        message: String,
        displayName: String? = null,
        nowMs: Long = System.currentTimeMillis(),
    ): VideoImportSnapshot = fail(code, message, displayName, nowMs)

    fun delete(id: String, nowMs: Long = System.currentTimeMillis()): VideoImportSnapshot {
        val imports = loadAndCleanup(nowMs)
        val target = imports.firstOrNull { it.id == id }
            ?: return VideoImportSnapshot(imports, loadError())
        if (target.storageMode == PendingVideoImport.STORAGE_PERSISTED_URI) {
            target.contentUri?.let { value ->
                runCatching {
                    appContext.contentResolver.releasePersistableUriPermission(
                        Uri.parse(value),
                        Intent.FLAG_GRANT_READ_URI_PERMISSION,
                    )
                }
            }
        } else {
            safeOwnedFile(target.filePath)?.delete()
        }
        val remaining = imports.filterNot { it.id == id }
        saveImports(remaining)
        return VideoImportSnapshot(remaining, loadError())
    }

    /** Privacy wipe: release only grants recorded by this plugin and delete only its cache root. */
    fun clearAll(): VideoImportSnapshot {
        val stored = runCatching { JSONArray(prefs.getString(PREF_IMPORTS, "[]")) }
            .getOrDefault(JSONArray())
        for (index in 0 until stored.length()) {
            val imported = stored.optJSONObject(index)?.let(PendingVideoImport::fromJson) ?: continue
            if (imported.storageMode == PendingVideoImport.STORAGE_PERSISTED_URI) {
                imported.contentUri?.let { value ->
                    runCatching {
                        appContext.contentResolver.releasePersistableUriPermission(
                            Uri.parse(value),
                            Intent.FLAG_GRANT_READ_URI_PERMISSION,
                        )
                    }
                }
            } else {
                safeOwnedFile(imported.filePath)?.let { file ->
                    if (file.exists() && !file.delete()) {
                        throw IOException("Could not delete a temporary imported video")
                    }
                }
            }
        }
        root.listFiles()?.forEach { file ->
            if (file.isFile && file.parentFile?.canonicalFile == root.canonicalFile && !file.delete()) {
                throw IOException("Could not delete temporary imported-video data")
            }
        }
        if (root.exists() && root.listFiles().isNullOrEmpty() && !root.delete()) {
            throw IOException("Could not remove temporary imported-video storage")
        }
        if (!prefs.edit().clear().commit()) {
            throw IOException("Could not clear imported-video metadata")
        }
        return VideoImportSnapshot(emptyList(), null)
    }

    fun clearError(nowMs: Long = System.currentTimeMillis()): VideoImportSnapshot {
        prefs.edit().remove(PREF_ERROR).commit()
        return VideoImportSnapshot(loadAndCleanup(nowMs), null)
    }

    private data class SourceInfo(
        val displayName: String,
        val mimeType: String,
        val declaredBytes: Long?,
    )

    private data class VideoMetadata(
        val durationMs: Long?,
        val recordedAt: String?,
        val recordedAtMs: Long?,
        val width: Int?,
        val height: Int?,
        val rotationDegrees: Int?,
        val codecMime: String?,
        val embeddedLocation: EmbeddedVideoLocation?,
    )

    private data class CopyResult(val bytes: Long, val sha256: String)

    private fun inspectSource(source: SharedVideoSource): SourceInfo {
        var rawName: String? = null
        var declaredBytes: Long? = null
        val signal = CancellationSignal()
        val timedOut = AtomicBoolean(false)
        ACTIVE_RESOLVER_SIGNALS.add(signal)
        val timeout = COPY_WATCHDOG.schedule({
            timedOut.set(true)
            signal.cancel()
        }, VIDEO_IMPORT_PROVIDER_SETUP_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        try {
            appContext.contentResolver.query(
                source.uri,
                arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE),
                null,
                null,
                null,
                signal,
            )?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if (nameIndex >= 0 && !cursor.isNull(nameIndex)) rawName = cursor.getString(nameIndex)
                    val sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE)
                    if (sizeIndex >= 0 && !cursor.isNull(sizeIndex)) {
                        cursor.getLong(sizeIndex).takeIf { it >= 0L }?.let { declaredBytes = it }
                    }
                }
            }
        } catch (error: Exception) {
            if (timedOut.get()) {
                throw VideoImportException(
                    "video_provider_timeout",
                    "The video provider took too long to respond. Try selecting the clip from Files.",
                )
            }
            throw error
        } finally {
            timeout.cancel(false)
            ACTIVE_RESOLVER_SIGNALS.remove(signal)
        }
        if (rawName.isNullOrBlank()) rawName = source.uri.lastPathSegment
        val displayName = safeVideoDisplayName(rawName)
        // Do not make a second unbounded provider Binder call for getType(). The system
        // picker/share MIME plus the bounded name is only a prefilter; readMetadata still
        // requires a real platform-readable video track before retaining the import.
        val mime = source.intentMimeType?.takeIf { it.isNotBlank() && it != "video/*" }
            ?: mimeForExtension(videoFileExtension(displayName))
            ?: if (source.sourceAction == Intent.ACTION_OPEN_DOCUMENT) "video/unknown"
            else source.intentMimeType?.takeIf { it.startsWith("video/") }
                ?: "application/octet-stream"
        return SourceInfo(displayName, mime.take(128), declaredBytes)
    }

    private fun copyBounded(uri: Uri, partial: File, budget: Long, displayName: String): CopyResult {
        if (budget <= 0L) throw limitException(VideoImportLimitFailure.PENDING_STORAGE_LIMIT, displayName)
        val signal = CancellationSignal()
        val descriptorReference = AtomicReference<ParcelFileDescriptor?>()
        val timedOut = AtomicBoolean(false)
        ACTIVE_RESOLVER_SIGNALS.add(signal)
        val timeout = COPY_WATCHDOG.schedule({
            timedOut.set(true)
            signal.cancel()
            descriptorReference.get()?.let { descriptor ->
                runCatching { descriptor.closeWithError("Shared-video copy timed out") }
            }
        }, VIDEO_IMPORT_COPY_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        var total = 0L
        val digest = MessageDigest.getInstance("SHA-256")
        var descriptor: ParcelFileDescriptor? = null
        try {
            val opened = appContext.contentResolver.openFileDescriptor(uri, "r", signal)
                ?: throw VideoImportException("unreadable_video", "$displayName could not be opened.", displayName)
            descriptor = opened
            descriptorReference.set(opened)
            ACTIVE_COPY_DESCRIPTORS.add(opened)
            if (timedOut.get()) {
                throw VideoImportException(
                    "shared_video_copy_timeout",
                    "$displayName took too long to open. Use Import video and select it from Files instead.",
                    displayName,
                )
            }
            ParcelFileDescriptor.AutoCloseInputStream(opened).buffered(256 * 1024).use { source ->
                FileOutputStream(partial).buffered(256 * 1024).use { output ->
                    val buffer = ByteArray(256 * 1024)
                    while (true) {
                        val read = source.read(buffer)
                        if (read < 0) break
                        if (read == 0) continue
                        total += read
                        if (total > budget) {
                            throw VideoImportException(
                                "shared_video_too_large",
                                oversizedMessage(displayName),
                                displayName,
                            )
                        }
                        digest.update(buffer, 0, read)
                        output.write(buffer, 0, read)
                    }
                }
            }
        } catch (error: VideoImportException) {
            throw error
        } catch (error: Exception) {
            if (timedOut.get()) {
                throw VideoImportException(
                    "shared_video_copy_timeout",
                    "$displayName took too long to copy. Use Import video and select it from Files instead.",
                    displayName,
                )
            }
            throw error
        } finally {
            timeout.cancel(false)
            ACTIVE_RESOLVER_SIGNALS.remove(signal)
            descriptor?.let {
                ACTIVE_COPY_DESCRIPTORS.remove(it)
                runCatching { it.close() }
            }
        }
        if (total <= 0L) {
            throw VideoImportException("empty_video", "$displayName is empty.", displayName)
        }
        return CopyResult(total, digest.digest().toHex())
    }

    /** Metadata failures are deliberately nonfatal: the actual video track check is separate. */
    private fun readMetadata(uri: Uri, displayName: String, requireVideoTrack: Boolean): VideoMetadata {
        var codecMime: String? = null
        var width: Int? = null
        var height: Int? = null
        var rotationDegrees: Int? = null
        var extractorDurationMs: Long? = null
        val extractor = MediaExtractor()
        try {
            if (uri.scheme == "file") {
                extractor.setDataSource(requireNotNull(uri.path))
            } else {
                extractor.setDataSource(appContext, uri, emptyMap())
            }
            for (index in 0 until extractor.trackCount) {
                val format = extractor.getTrackFormat(index)
                val mime = format.getString(MediaFormat.KEY_MIME).orEmpty()
                if (!mime.startsWith("video/")) continue
                codecMime = mime.take(128)
                width = format.safeInt(MediaFormat.KEY_WIDTH)
                height = format.safeInt(MediaFormat.KEY_HEIGHT)
                rotationDegrees = format.safeInt(MediaFormat.KEY_ROTATION)
                format.safeLong(MediaFormat.KEY_DURATION)?.let { extractorDurationMs = it / 1000L }
                break
            }
        } catch (_: Exception) {
            // A retriever below may still recognise a valid container that MediaExtractor does not.
        } finally {
            extractor.release()
        }

        var durationMs = extractorDurationMs
        var recordedAt: String? = null
        var recordedAtMs: Long? = null
        var embeddedLocation: EmbeddedVideoLocation? = null
        var retrieverHasVideo = false
        val retriever = MediaMetadataRetriever()
        try {
            if (uri.scheme == "file") {
                retriever.setDataSource(requireNotNull(uri.path))
            } else {
                retriever.setDataSource(appContext, uri)
            }
            retrieverHasVideo = retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_HAS_VIDEO) == "yes"
            if (durationMs == null) {
                durationMs = retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)
                    ?.toLongOrNull()?.takeIf { it >= 0L }
            }
            if (width == null) {
                width = retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_WIDTH)
                    ?.toIntOrNull()?.takeIf { it > 0 }
            }
            if (height == null) {
                height = retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_HEIGHT)
                    ?.toIntOrNull()?.takeIf { it > 0 }
            }
            if (rotationDegrees == null) {
                rotationDegrees = retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_ROTATION)
                    ?.toIntOrNull()?.takeIf { it in setOf(0, 90, 180, 270) }
            }
            recordedAt = retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DATE)
                ?.trim()?.take(128)?.takeIf(String::isNotBlank)
            recordedAtMs = parseVideoRecordedAtMs(recordedAt)
            embeddedLocation = parseDecimalIso6709(
                retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_LOCATION),
            )
        } catch (_: Exception) {
            // Optional metadata is allowed to be absent or malformed.
        } finally {
            runCatching { retriever.release() }
        }

        if (requireVideoTrack && codecMime == null && !retrieverHasVideo) {
            throw VideoImportException(
                "unreadable_video",
                "$displayName does not contain a readable video track.",
                displayName,
            )
        }
        return VideoMetadata(
            durationMs = durationMs,
            recordedAt = recordedAt,
            recordedAtMs = recordedAtMs,
            width = width,
            height = height,
            rotationDegrees = rotationDegrees,
            codecMime = codecMime,
            embeddedLocation = embeddedLocation,
        )
    }

    private fun loadAndCleanup(nowMs: Long): List<PendingVideoImport> {
        root.mkdirs()
        val stored = runCatching { JSONArray(prefs.getString(PREF_IMPORTS, "[]")) }
            .getOrDefault(JSONArray())
        val valid = buildList {
            for (index in 0 until stored.length()) {
                val imported = stored.optJSONObject(index)?.let(PendingVideoImport::fromJson) ?: continue
                if (imported.storageMode == PendingVideoImport.STORAGE_PERSISTED_URI) {
                    val uri = imported.contentUri?.let(Uri::parse)
                    val hasGrant = uri != null && appContext.contentResolver.persistedUriPermissions
                        .any { permission -> permission.isReadPermission && permission.uri == uri }
                    if (!hasGrant || isExpiredPersistedVideoImport(imported.createdAtMs, nowMs)) {
                        if (hasGrant) {
                            runCatching {
                                appContext.contentResolver.releasePersistableUriPermission(
                                    requireNotNull(uri),
                                    Intent.FLAG_GRANT_READ_URI_PERMISSION,
                                )
                            }
                        }
                        continue
                    }
                } else {
                    val file = safeOwnedFile(imported.filePath)
                    if (file == null || !file.isFile || file.length() != imported.bytes ||
                        isExpiredVideoImport(imported.createdAtMs, nowMs)
                    ) {
                        file?.delete()
                        continue
                    }
                }
                add(imported)
            }
        }
        saveImports(valid)

        val ownedPaths = valid.mapNotNullTo(mutableSetOf()) { imported ->
            imported.filePath?.let { File(it).canonicalPath }
        }
        root.listFiles()?.forEach { file ->
            val isOldPartial = file.extension == "part" && nowMs - file.lastModified() >= ORPHAN_PART_RETENTION_MS
            val isOldOrphan = file.extension != "part" &&
                file.canonicalPath !in ownedPaths && nowMs - file.lastModified() >= ORPHAN_PART_RETENTION_MS
            if (isOldPartial || isOldOrphan) file.delete()
        }
        return valid
    }

    private fun safeOwnedFile(path: String?): File? = path?.let { value -> runCatching {
        val candidate = File(value).canonicalFile
        val canonicalRoot = root.canonicalFile
        candidate.takeIf { it.parentFile == canonicalRoot }
    }.getOrNull() }

    private fun saveImports(imports: List<PendingVideoImport>): Boolean {
        val json = JSONArray().also { array -> imports.forEach { array.put(it.persistedJson()) } }
        return prefs.edit().putString(PREF_IMPORTS, json.toString()).commit()
    }

    private fun loadError(): VideoImportError? = prefs.getString(PREF_ERROR, null)?.let { value ->
        runCatching { VideoImportError.fromJson(JSONObject(value)) }.getOrNull()
    }

    private fun fail(code: String, message: String, name: String?, nowMs: Long): VideoImportSnapshot {
        val error = VideoImportError(code, message.take(512), name?.take(120), nowMs)
        prefs.edit().putString(PREF_ERROR, error.json().toString()).commit()
        return VideoImportSnapshot(loadAndCleanup(nowMs), error)
    }

    private fun availableBytes(): Long = runCatching { StatFs(root.absolutePath).availableBytes }
        .getOrDefault(0L)

    private fun limitException(failure: VideoImportLimitFailure, name: String): VideoImportException = when (failure) {
        VideoImportLimitFailure.ITEM_TOO_LARGE -> VideoImportException(
            "shared_video_too_large",
            oversizedMessage(name),
            name,
        )
        VideoImportLimitFailure.PENDING_STORAGE_LIMIT -> VideoImportException(
            "pending_video_limit",
            "Temporary shared-video storage is full. Finish or discard a pending video, or use Import video for a large dashcam file.",
            name,
        )
        VideoImportLimitFailure.LOW_DEVICE_STORAGE -> VideoImportException(
            "low_storage",
            "There is not enough free space to copy $name. Use Import video and select it from Files instead.",
            name,
        )
    }

    private fun oversizedMessage(name: String) =
        "$name is over the 512 MB external-share limit. Open Pothole Reporter, tap Import video, and select it from Files; that route does not make a full native copy."

    companion object {
        private const val PREFS_NAME = "video_import_handoff_v1"
        private const val PREF_IMPORTS = "pending_imports"
        private const val PREF_ERROR = "last_error"
        private const val DIRECTORY_NAME = "shared-video-imports"
        private const val ORPHAN_PART_RETENTION_MS = 60L * 60L * 1000L
        private val ACTIVE_RESOLVER_SIGNALS = ConcurrentHashMap.newKeySet<CancellationSignal>()
        private val ACTIVE_COPY_DESCRIPTORS = ConcurrentHashMap.newKeySet<ParcelFileDescriptor>()
        private val COPY_WATCHDOG = Executors.newSingleThreadScheduledExecutor { runnable ->
            Thread(runnable, "PotholeVideoCopyWatchdog").apply { isDaemon = true }
        }

        /** Interrupt an external provider read before privacy wipe or Activity teardown. */
        fun cancelActiveCopies() {
            ACTIVE_RESOLVER_SIGNALS.toList().forEach(CancellationSignal::cancel)
            ACTIVE_COPY_DESCRIPTORS.toList().forEach { descriptor ->
                runCatching { descriptor.closeWithError("Shared-video import cancelled") }
            }
        }
    }
}

private fun sourceFingerprint(uri: Uri): String = MessageDigest.getInstance("SHA-256")
    .digest(uri.toString().toByteArray(Charsets.UTF_8))
    .toHex()

private fun ByteArray.toHex(): String =
    joinToString("") { (it.toInt() and 0xff).toString(16).padStart(2, '0') }

private fun JSONObject.optLongOrNull(key: String): Long? =
    if (!has(key) || isNull(key)) null else optLong(key).takeIf { it >= 0L }

private fun JSONObject.optIntOrNull(key: String): Int? =
    if (!has(key) || isNull(key)) null else optInt(key)

private fun JSONObject.optBoundedString(key: String, maxLength: Int = 128): String? =
    if (!has(key) || isNull(key)) null else optString(key).trim().take(maxLength).takeIf(String::isNotBlank)

private fun MediaFormat.safeInt(key: String): Int? =
    if (containsKey(key)) runCatching { getInteger(key) }.getOrNull() else null

private fun MediaFormat.safeLong(key: String): Long? =
    if (containsKey(key)) runCatching { getLong(key) }.getOrNull() else null

private fun mimeForExtension(extension: String?): String? = when (extension) {
    "mp4", "m4v" -> "video/mp4"
    "mov" -> "video/quicktime"
    "webm" -> "video/webm"
    "mkv" -> "video/x-matroska"
    "avi" -> "video/x-msvideo"
    "3gp", "3g2" -> "video/3gpp"
    "ts", "mts", "m2ts" -> "video/mp2t"
    else -> null
}

private fun extensionForMime(mime: String): String? = when (mime.substringBefore(';').lowercase()) {
    "video/mp4", "application/mp4", "application/mpeg4" -> "mp4"
    "video/quicktime" -> "mov"
    "video/webm" -> "webm"
    "video/x-matroska" -> "mkv"
    "video/x-msvideo", "video/avi" -> "avi"
    "video/3gpp", "video/3gpp2" -> "3gp"
    "video/mp2t" -> "ts"
    else -> null
}
