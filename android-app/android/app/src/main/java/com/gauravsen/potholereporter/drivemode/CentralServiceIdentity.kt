package com.gauravsen.potholereporter.drivemode

import android.content.Context
import android.util.Base64
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.math.BigInteger
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.MessageDigest
import java.security.Signature
import java.security.interfaces.ECPublicKey
import java.security.spec.ECGenParameterSpec
import java.net.URI
import java.util.concurrent.TimeUnit
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties

/**
 * One pseudonymous identity for central-service calls made by the APK.
 *
 * The private key is generated non-exportably in Android Keystore. The WebView uses
 * the same key through DriveModePlugin, so manual reports and background Drive Mode do
 * not look like two installations in aggregate metrics.
 */
class CentralServiceIdentity(private val context: Context) {
    data class SignedHeaders(
        val installId: String,
        val timestamp: String,
        val idempotencyKey: String,
        val signature: String,
    )

    companion object {
        private const val KEY_ALIAS = "pothole_central_identity_v1"
        const val DEFAULT_SERVICE_URL = "https://ffjvg34k07.execute-api.ap-south-1.amazonaws.com"
        const val PREFS = "pothole_central_service"
        private const val CONNECT_TIMEOUT_MS = 15_000L
        private val IDENTITY_LOCK = Any()

        fun sha256Hex(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
            .digest(bytes).joinToString("") { "%02x".format(it.toInt() and 0xff) }

        fun canonicalRequest(
            method: String,
            path: String,
            timestamp: String,
            idempotencyKey: String,
            body: ByteArray,
        ): String = listOf(
            method.uppercase(),
            path,
            timestamp,
            idempotencyKey,
            sha256Hex(body),
        ).joinToString("\n")

        /** Return a canonical origin so request URLs and signed pathnames cannot diverge. */
        fun normalizeServiceUrl(serviceUrl: String): String {
            val base = serviceUrl.trim().trimEnd('/')
            val uri = runCatching { URI(base) }.getOrNull()
                ?: throw IllegalArgumentException("The central service URL is invalid")
            val host = uri.host?.lowercase()
            val secure = uri.scheme.equals("https", ignoreCase = true)
            val localDevelopment = uri.scheme.equals("http", ignoreCase = true)
                && host in setOf("10.0.2.2", "127.0.0.1", "localhost")
            require((secure || localDevelopment)
                && !host.isNullOrBlank()
                && uri.rawUserInfo == null
                && uri.rawPath.orEmpty().isEmpty()
                && uri.rawQuery == null
                && uri.rawFragment == null
                && (uri.port == -1 || uri.port in 1..65535)) {
                "The central service must be an HTTPS origin"
            }
            return base
        }

        /** Forget the local pseudonymous identity and its deployment binding. */
        fun reset(context: Context) = synchronized(IDENTITY_LOCK) {
            val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
            if (store.containsAlias(KEY_ALIAS)) store.deleteEntry(KEY_ALIAS)
            check(context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit().clear().commit()) { "Could not clear central-service preferences" }
        }

        private fun unsigned32(value: BigInteger): ByteArray {
            val source = value.toByteArray()
            val withoutSign = if (source.size > 32 && source[0] == 0.toByte()) {
                source.copyOfRange(1, source.size)
            } else source
            require(withoutSign.size <= 32) { "P-256 coordinate is too large" }
            return ByteArray(32).also { out ->
                withoutSign.copyInto(out, 32 - withoutSign.size)
            }
        }
    }

    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    private val client = OkHttpClient.Builder()
        .connectTimeout(CONNECT_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .readTimeout(CONNECT_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .writeTimeout(CONNECT_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .build()

    private fun keyPair(): KeyPair = synchronized(IDENTITY_LOCK) {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        val existing = store.getEntry(KEY_ALIAS, null) as? KeyStore.PrivateKeyEntry
        if (existing != null) return@synchronized KeyPair(
            existing.certificate.publicKey,
            existing.privateKey,
        )

        val generator = KeyPairGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_EC,
            "AndroidKeyStore",
        )
        generator.initialize(
            KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_SIGN)
                .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256)
                .setUserAuthenticationRequired(false)
                .build(),
        )
        generator.generateKeyPair()
    }

    private fun rawPublicKey(pair: KeyPair = keyPair()): ByteArray {
        val point = (pair.public as ECPublicKey).w
        return byteArrayOf(0x04) + unsigned32(point.affineX) + unsigned32(point.affineY)
    }

    fun publicKeyBase64(): String = Base64.encodeToString(rawPublicKey(), Base64.NO_WRAP)

    /** Forget only a deployment registration, retaining the non-exportable key. */
    fun forgetRegistration(serviceUrl: String) = synchronized(IDENTITY_LOCK) {
        val base = normalizeServiceUrl(serviceUrl)
        if (prefs.getString("registered_url", null) == base) {
            check(prefs.edit().remove("registered_url").remove("install_id").commit()) {
                "Could not clear stale central-service registration"
            }
        }
    }

    /** Register this public key with a deployment, once per service URL. */
    fun ensureRegistered(serviceUrl: String): String = synchronized(IDENTITY_LOCK) {
        val base = normalizeServiceUrl(serviceUrl)
        val rawKey = rawPublicKey()
        val expectedId = sha256Hex(rawKey).take(32)
        val cachedUrl = prefs.getString("registered_url", null)
        val cachedId = prefs.getString("install_id", null)
        if (cachedUrl == base && cachedId == expectedId) return@synchronized cachedId

        val body = JSONObject().put(
            "public_key",
            Base64.encodeToString(rawKey, Base64.NO_WRAP),
        ).toString()
            .toByteArray(Charsets.UTF_8)
        val request = Request.Builder()
            .url("$base/v1/installations")
            .post(body.toRequestBody("application/json; charset=utf-8".toMediaType()))
            .build()
        client.newCall(request).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                val error = runCatching { JSONObject(text) }.getOrNull()
                val code = error?.optString("error")?.takeIf { it.isNotBlank() }
                    ?: "http_${response.code}"
                val message = error?.optString("message")?.takeIf { it.isNotBlank() }
                    ?: "Could not register this installation (${response.code})"
                throw CentralServiceException(
                    response.code,
                    code,
                    message,
                    error?.optJSONObject("details"),
                )
            }
            val result = runCatching { JSONObject(text) }.getOrElse {
                throw CentralServiceException(
                    502,
                    "bad_service_response",
                    "Central service returned invalid registration JSON",
                )
            }
            val installId = result.optString("install_id")
            if (installId.isBlank() || installId != expectedId) {
                throw CentralServiceException(
                    502,
                    "bad_service_response",
                    "Central service returned an invalid installation id",
                )
            }
            prefs.edit().putString("registered_url", base).putString("install_id", installId).apply()
            return@synchronized installId
        }
    }

    fun sign(
        serviceUrl: String,
        method: String,
        path: String,
        body: ByteArray,
        idempotencyKey: String = "",
        timestamp: String = System.currentTimeMillis().toString(),
    ): SignedHeaders {
        val installId = ensureRegistered(serviceUrl)
        val canonical = canonicalRequest(method, path, timestamp, idempotencyKey, body)
        val signer = Signature.getInstance("SHA256withECDSA")
        signer.initSign(keyPair().private)
        signer.update(canonical.toByteArray(Charsets.UTF_8))
        // Android returns ASN.1 DER. The service accepts DER as well as the browser's
        // WebCrypto P1363 form, so the exact same identity works in both request paths.
        val signature = Base64.encodeToString(signer.sign(), Base64.NO_WRAP)
        return SignedHeaders(installId, timestamp, idempotencyKey, signature)
    }
}
