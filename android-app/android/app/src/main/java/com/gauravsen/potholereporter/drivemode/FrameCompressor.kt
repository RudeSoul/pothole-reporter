package com.gauravsen.potholereporter.drivemode

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.ColorMatrix
import android.graphics.ColorMatrixColorFilter
import android.graphics.Paint
import android.util.Base64
import java.io.ByteArrayOutputStream
import kotlin.math.max
import kotlin.math.min

/** Prepares the single primary Drive Mode frame sent to the detector. */
object FrameCompressor {
    data class PreparedImage(
        val analysisBase64: String,
        val thumbnailJpeg: ByteArray,
        val evidenceJpeg: ByteArray,
    )

    fun prepare(primaryJpeg: ByteArray): PreparedImage {
        require(primaryJpeg.isNotEmpty()) { "Primary frame must not be empty" }
        val analysisBase64 = processImage(
            jpegBytes = primaryJpeg,
            band = LlmContractGenerated.DRIVE_ROAD_BAND.toFloat(),
            maxDim = LlmContractGenerated.DRIVE_MAX_DIMENSION,
            quality = LlmContractGenerated.DRIVE_JPEG_QUALITY,
            boost = LlmContractGenerated.DRIVE_ADAPTIVE_BRIGHTNESS,
        )

        val thumbnailJpeg = generateThumbnail(primaryJpeg)

        return PreparedImage(
            analysisBase64 = analysisBase64,
            thumbnailJpeg = thumbnailJpeg,
            evidenceJpeg = primaryJpeg,
        )
    }

    private fun processImage(
        jpegBytes: ByteArray,
        band: Float,
        maxDim: Int,
        quality: Int,
        boost: Boolean
    ): String {
        val originalBitmap = BitmapFactory.decodeByteArray(jpegBytes, 0, jpegBytes.size)
        requireNotNull(originalBitmap) { "Failed to decode JPEG" }

        try {
            // 1. Crop to bottom band
            val cropHeight = (originalBitmap.height * band).toInt()
            val startY = originalBitmap.height - cropHeight
            val croppedBitmap = if (band < 1.0f) {
                Bitmap.createBitmap(originalBitmap, 0, startY, originalBitmap.width, cropHeight)
            } else {
                originalBitmap
            }

            try {
                // 2. Scale down so largest dimension doesn't exceed maxDim
                val maxOriginalDim = max(croppedBitmap.width, croppedBitmap.height)
                val scaledBitmap = if (maxOriginalDim > maxDim) {
                    val scale = maxDim.toFloat() / maxOriginalDim
                    val newWidth = (croppedBitmap.width * scale).toInt()
                    val newHeight = (croppedBitmap.height * scale).toInt()
                    Bitmap.createScaledBitmap(croppedBitmap, newWidth, newHeight, true)
                } else {
                    croppedBitmap
                }

                try {
                    // 3. Brightness boost
                    val finalBitmap = if (boost) {
                        applyBrightnessBoostIfNeeded(scaledBitmap)
                    } else {
                        scaledBitmap
                    }

                    try {
                        // 4. Compress to JPEG
                        val outputStream = ByteArrayOutputStream()
                        finalBitmap.compress(Bitmap.CompressFormat.JPEG, quality, outputStream)
                        val compressedBytes = outputStream.toByteArray()

                        // 5. Convert to Base64 data URL
                        val base64String = Base64.encodeToString(compressedBytes, Base64.NO_WRAP)
                        return "data:image/jpeg;base64,$base64String"
                    } finally {
                        if (finalBitmap !== scaledBitmap) {
                            finalBitmap.recycle()
                        }
                    }
                } finally {
                    if (scaledBitmap !== croppedBitmap && scaledBitmap !== originalBitmap) {
                        scaledBitmap.recycle()
                    }
                }
            } finally {
                if (croppedBitmap !== originalBitmap) {
                    croppedBitmap.recycle()
                }
            }
        } finally {
            originalBitmap.recycle()
        }
    }

    private fun applyBrightnessBoostIfNeeded(bitmap: Bitmap): Bitmap {
        val width = bitmap.width
        val height = bitmap.height

        // Sample the canonical number of pixels used by browser and eval transforms.
        val totalPixels = width * height
        val step = max(1, kotlin.math.sqrt(
            totalPixels.toFloat() / LlmContractGenerated.LUMINANCE_TARGET_SAMPLES
        ).toInt())

        var luminanceSum = 0f
        var count = 0
        var darkCount = 0
        var brightCount = 0

        for (y in 0 until height step step) {
            for (x in 0 until width step step) {
                val pixel = bitmap.getPixel(x, y)
                val r = (pixel shr 16) and 0xFF
                val g = (pixel shr 8) and 0xFF
                val b = pixel and 0xFF

                // 0.2126*R + 0.7152*G + 0.0722*B
                val luminance = 0.2126f * r + 0.7152f * g + 0.0722f * b
                luminanceSum += luminance
                count++

                if (luminance < LlmContractGenerated.LUMINANCE_DARK_PIXEL_THRESHOLD) darkCount++
                if (luminance > LlmContractGenerated.LUMINANCE_BRIGHT_PIXEL_THRESHOLD) brightCount++
            }
        }

        if (count == 0) return bitmap

        val mean = luminanceSum / count
        val brightFraction = brightCount.toFloat() / count

        if (mean < LlmContractGenerated.LUMINANCE_MEAN_THRESHOLD
            && brightFraction < LlmContractGenerated.LUMINANCE_BRIGHT_FRACTION_THRESHOLD) {
            val brightnessLift = min(
                LlmContractGenerated.LUMINANCE_MAXIMUM_LIFT,
                max(
                    LlmContractGenerated.LUMINANCE_MINIMUM_LIFT,
                    LlmContractGenerated.LUMINANCE_TARGET_MEAN /
                        max(LlmContractGenerated.LUMINANCE_MEAN_FLOOR, mean),
                ),
            )
            val contrast = LlmContractGenerated.LUMINANCE_CONTRAST

            val resultBitmap = Bitmap.createBitmap(width, height, bitmap.config ?: Bitmap.Config.ARGB_8888)
            val canvas = Canvas(resultBitmap)
            val paint = Paint()

            val cm = ColorMatrix(floatArrayOf(
                contrast * brightnessLift, 0f, 0f, 0f, 0f,
                0f, contrast * brightnessLift, 0f, 0f, 0f,
                0f, 0f, contrast * brightnessLift, 0f, 0f,
                0f, 0f, 0f, 1f, 0f
            ))
            paint.colorFilter = ColorMatrixColorFilter(cm)
            canvas.drawBitmap(bitmap, 0f, 0f, paint)

            return resultBitmap
        }

        return bitmap
    }

    private fun generateThumbnail(jpegBytes: ByteArray): ByteArray {
        val originalBitmap = BitmapFactory.decodeByteArray(jpegBytes, 0, jpegBytes.size)
        requireNotNull(originalBitmap) { "Failed to decode JPEG for thumbnail" }

        try {
            val maxDim = 256
            val maxOriginalDim = max(originalBitmap.width, originalBitmap.height)

            val scaledBitmap = if (maxOriginalDim > maxDim) {
                val scale = maxDim.toFloat() / maxOriginalDim
                val newWidth = (originalBitmap.width * scale).toInt()
                val newHeight = (originalBitmap.height * scale).toInt()
                Bitmap.createScaledBitmap(originalBitmap, newWidth, newHeight, true)
            } else {
                originalBitmap
            }

            try {
                val outputStream = ByteArrayOutputStream()
                scaledBitmap.compress(Bitmap.CompressFormat.JPEG, 75, outputStream)
                return outputStream.toByteArray()
            } finally {
                if (scaledBitmap !== originalBitmap) {
                    scaledBitmap.recycle()
                }
            }
        } finally {
            originalBitmap.recycle()
        }
    }
}
