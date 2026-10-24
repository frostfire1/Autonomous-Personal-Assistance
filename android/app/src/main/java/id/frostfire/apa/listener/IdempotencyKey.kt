package id.frostfire.apa.listener

import java.security.MessageDigest

/** The server stores an event once per key, so an offline buffer can resend freely (blueprint 4.1, phase 1 gate). */
object IdempotencyKey {
    fun of(packageName: String, postTimeMillis: Long, text: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest("$packageName|$postTimeMillis|$text".toByteArray())
        return digest.joinToString("") { "%02x".format(it) }
    }
}
