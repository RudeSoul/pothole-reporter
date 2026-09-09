package com.gauravsen.potholereporter.drivemode

import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import java.io.ByteArrayOutputStream
import java.util.concurrent.atomic.AtomicBoolean

data class CapturedFrame(
    val jpeg: ByteArray,
    val width: Int,
    val height: Int,
    val capturedAtMs: Long
) {
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (javaClass != other?.javaClass) return false

        other as CapturedFrame

        if (!jpeg.contentEquals(other.jpeg)) return false
        if (width != other.width) return false
        if (height != other.height) return false
        if (capturedAtMs != other.capturedAtMs) return false

        return true
    }

    override fun hashCode(): Int {
        var result = jpeg.contentHashCode()
        result = 31 * result + width
        result = 31 * result + height
        result = 31 * result + capturedAtMs.hashCode()
        return result
    }
}

class FrameAnalyzer(
    private val onFrameCaptured: (CapturedFrame) -> Unit
) : ImageAnalysis.Analyzer {

    private val captureRequested = AtomicBoolean(false)
    @Volatile
    private var isCapturing = false

    fun requestCapture() {
        captureRequested.set(true)
    }

    fun isCapturing(): Boolean {
        return isCapturing || captureRequested.get()
    }

    @Synchronized
    override fun analyze(image: ImageProxy) {
        try {
            if (!captureRequested.getAndSet(false)) return
            isCapturing = true
            processFrame(image, System.currentTimeMillis())?.let(onFrameCaptured)
        } finally {
            isCapturing = false
            image.close()
        }
    }

    /**
     * Cancel a requested capture that CameraX has not delivered yet. This method and
     * [analyze] share the same monitor, so Stop either waits for an in-progress JPEG and
     * its callback or cancels before conversion begins—never halfway through a frame.
     */
    @Synchronized
    fun cancelPendingCapture(): Boolean {
        return captureRequested.getAndSet(false)
    }

    private fun processFrame(image: ImageProxy, captureTimeMs: Long): CapturedFrame? {
        if (image.format != ImageFormat.YUV_420_888) {
            return null
        }

        val width = image.width
        val height = image.height

        val yuvBytes = yuv420888ToNv21(image)
        val jpegBytes = nv21ToJpeg(yuvBytes, width, height)

        return CapturedFrame(
            jpeg = jpegBytes,
            width = width,
            height = height,
            capturedAtMs = captureTimeMs
        )
    }

    private fun yuv420888ToNv21(image: ImageProxy): ByteArray {
        val yPlane = image.planes[0]
        val uPlane = image.planes[1]
        val vPlane = image.planes[2]

        val yBuffer = yPlane.buffer
        val uBuffer = uPlane.buffer
        val vBuffer = vPlane.buffer

        val ySize = yBuffer.remaining()
        val uSize = uBuffer.remaining()
        val nv21 = ByteArray(ySize + image.width * image.height / 2)

        // U and V are swapped in NV21
        yBuffer.get(nv21, 0, ySize)

        // NV21 requires V U interleave
        // Depending on device, U/V planes might already be interleaved.
        // For simplicity, we use a robust method if rowStride allows, but YuvImage needs standard NV21.
        val vRowStride = vPlane.rowStride
        val vPixelStride = vPlane.pixelStride
        val uRowStride = uPlane.rowStride
        val uPixelStride = uPlane.pixelStride

        var pos = ySize

        if (vPixelStride == 2 && uPixelStride == 2 && uPlane.buffer[0] == vPlane.buffer[1]) {
            // Already interleaved in NV21 (V U V U), common case
            // The length of the interleaved buffer is slightly less than vSize + uSize
            val length = uSize.coerceAtMost(vBuffer.remaining())
            vBuffer.get(nv21, ySize, length)
        } else {
            // Slow path, manual interleave
            for (row in 0 until image.height / 2) {
                for (col in 0 until image.width / 2) {
                    val vIdx = row * vRowStride + col * vPixelStride
                    val uIdx = row * uRowStride + col * uPixelStride

                    nv21[pos++] = vBuffer[vIdx]
                    nv21[pos++] = uBuffer[uIdx]
                }
            }
        }
        return nv21
    }

    private fun nv21ToJpeg(nv21: ByteArray, width: Int, height: Int): ByteArray {
        val yuvImage = YuvImage(nv21, ImageFormat.NV21, width, height, null)
        val out = ByteArrayOutputStream()
        yuvImage.compressToJpeg(Rect(0, 0, width, height), 90, out)
        return out.toByteArray()
    }
}
