package com.gauravsen.potholereporter.drivemode

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class CentralServiceIdentityTest {
    @Test
    fun acceptsProductionAndLocalDevelopmentOrigins() {
        assertEquals(
            "https://service.example:8443",
            CentralServiceIdentity.normalizeServiceUrl(" https://service.example:8443/ "),
        )
        assertEquals(
            "http://10.0.2.2:8787",
            CentralServiceIdentity.normalizeServiceUrl("http://10.0.2.2:8787/"),
        )
    }

    @Test
    fun rejectsAnythingThatIsNotAnAllowedOrigin() {
        listOf(
            "https:///missing-host",
            "https://user@example.com",
            "https://example.com/prefix",
            "https://example.com?query=yes",
            "https://example.com#fragment",
            "http://example.com",
        ).forEach { candidate ->
            assertThrows(IllegalArgumentException::class.java) {
                CentralServiceIdentity.normalizeServiceUrl(candidate)
            }
        }
    }
}
