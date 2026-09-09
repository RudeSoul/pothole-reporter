package com.gauravsen.potholereporter

import android.content.Intent
import android.net.Uri
import android.util.Base64
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.getcapacitor.JSArray
import de.einfachhans.emailcomposer.EmailComposer
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/** Verifies the native intent used by the app's one-photo complaint email. */
@RunWith(AndroidJUnit4::class)
class EmailAttachmentIntentTest {
    @Test
    fun singlePhotoIsReadableByTheSelectedEmailApp() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val expected = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 1, 2, 3, 0xff.toByte(), 0xd9.toByte())
        val attachments = JSArray().apply {
            put(
                JSONObject()
                    .put("type", "base64")
                    .put("name", "road-damage.jpg")
                    .put("path", Base64.encodeToString(expected, Base64.NO_WRAP)),
            )
        }
        val baseIntent = Intent(Intent.ACTION_SENDTO, Uri.parse("mailto:"))

        // The dependency keeps attachment conversion private; invoke that exact production
        // boundary so this test also catches a missing FileProvider path after upgrades.
        val method = EmailComposer::class.java.getDeclaredMethod(
            "getIntentAccordingToAttachments",
            JSArray::class.java,
            Intent::class.java,
        ).apply { isAccessible = true }
        val result = method.invoke(EmailComposer(context), attachments, baseIntent) as Intent
        @Suppress("DEPRECATION")
        val emailIntent = if (result.action == Intent.ACTION_CHOOSER) {
            result.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
        } else {
            result
        }

        assertNotNull(emailIntent)
        val sendIntent = requireNotNull(emailIntent)
        assertEquals(Intent.ACTION_SEND, sendIntent.action)
        assertEquals("image/jpeg", sendIntent.type)
        assertTrue(
            "The recipient needs a temporary FileProvider read grant",
            sendIntent.flags and Intent.FLAG_GRANT_READ_URI_PERMISSION != 0,
        )
        @Suppress("DEPRECATION")
        val stream = sendIntent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)
        assertNotNull(stream)
        assertNotNull(sendIntent.clipData)
        assertEquals(stream, sendIntent.clipData!!.getItemAt(0).uri)
        val actual = context.contentResolver.openInputStream(requireNotNull(stream)).use {
            requireNotNull(it).readBytes()
        }
        assertArrayEquals(expected, actual)
    }
}
