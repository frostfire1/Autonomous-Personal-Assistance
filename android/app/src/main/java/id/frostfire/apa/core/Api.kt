package id.frostfire.apa.core

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.time.Instant
import java.util.concurrent.TimeUnit
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/** The server's HTTP API. Every call carries the API key; nothing is sent when the app is not configured. */
class Api(private val cfg: Config) {
    data class Result(val code: Int, val body: String) {
        val ok get() = code in 200..299
    }

    private fun builder(path: String): Request.Builder =
        Request.Builder().url(cfg.serverUrl + path).header("X-API-Key", cfg.apiKey)

    private suspend fun call(req: Request): Result = withContext(Dispatchers.IO) {
        client.newCall(sign(req)).execute().use { Result(it.code, it.body?.string().orEmpty()) }
    }

    /** HMAC-SHA256 over "timestamp.METHOD.path." plus the exact body bytes, matching the server's `sign`. */
    private fun sign(req: Request): Request {
        val secret = cfg.hmacSecret
        if (secret.isBlank()) return req
        val body = okio.Buffer().also { req.body?.writeTo(it) }.readByteArray()
        val ts = (System.currentTimeMillis() / 1000).toString()
        val mac = Mac.getInstance("HmacSHA256").apply { init(SecretKeySpec(secret.toByteArray(), "HmacSHA256")) }
        mac.update("$ts.${req.method.uppercase()}.${req.url.encodedPath}.".toByteArray())
        val sig = mac.doFinal(body).joinToString("") { "%02x".format(it) }
        return req.newBuilder().header("X-Timestamp", ts).header("X-Signature", sig).build()
    }

    suspend fun postNotification(key: String, pkg: String, title: String?, text: String, postedAtMillis: Long): Result {
        val body = JSONObject()
            .put("idempotency_key", key).put("package_name", pkg).put("text", text)
            .put("posted_at", Instant.ofEpochMilli(postedAtMillis).toString())
        title?.let { body.put("title", it) }
        return call(builder("/ingest/notification").post(body.toString().toRequestBody(JSON)).build())
    }

    suspend fun briefing(kind: String): Result = call(builder("/briefing/$kind").get().build())

    /** One briefing section as MP3 in the server's natural voice; null on any failure (the app then speaks it itself). */
    suspend fun audio(id: Long, index: Int): ByteArray? = withContext(Dispatchers.IO) {
        runCatching {
            client.newCall(sign(builder("/briefing/$id/audio/$index").get().build())).execute().use {
                if (it.isSuccessful) it.body?.bytes()?.takeIf { b -> b.size > 500 } else null
            }
        }.getOrNull()
    }

    /** The whole briefing as ONE MP3 (a single voice call on the server); null on any failure (the app then speaks it itself). */
    suspend fun audioWhole(id: Long): ByteArray? = withContext(Dispatchers.IO) {
        runCatching {
            client.newCall(sign(builder("/briefing/$id/audio").get().build())).execute().use {
                if (it.isSuccessful) it.body?.bytes()?.takeIf { b -> b.size > 500 } else null
            }
        }.getOrNull()
    }

    /** The full current selection of extra banking apps; the server stores it and answers with what it accepted. */
    suspend fun postPackages(apps: List<Pair<String, String>>): Result {
        val arr = org.json.JSONArray()
        apps.forEach { (pkg, label) -> arr.put(JSONObject().put("package", pkg).put("label", label)) }
        return call(builder("/ingest/packages").post(JSONObject().put("apps", arr).toString().toRequestBody(JSON)).build())
    }

    /** Ask the server to also send the complete text briefing to Telegram (the manual "play now" buttons do this). */
    suspend fun sendBriefingToTelegram(): Result = call(builder("/briefing/telegram").post("".toRequestBody(JSON)).build())

    /** Tells the server the app is alive, whether the notification listener is connected, and how many rows wait unsent. */
    suspend fun heartbeat(listener: Boolean, pending: Int, version: String): Result {
        val body = JSONObject().put("listener", listener).put("pending", pending).put("version", version)
        return call(builder("/ingest/heartbeat").post(body.toString().toRequestBody(JSON)).build())
    }

    /** One day of app usage from the ActivityWatch app on this phone: app names and seconds only. */
    suspend fun putUsage(body: JSONObject): Result = call(builder("/ingest/usage").post(body.toString().toRequestBody(JSON)).build())

    suspend fun getPrefs(): Result = call(builder("/prefs").get().build())

    suspend fun putPrefs(body: JSONObject): Result = call(builder("/prefs").put(body.toString().toRequestBody(JSON)).build())

    /** The phone's coarse position for the weather. The server rounds it to about a kilometre and ignores it unless GPS mode is on. */
    suspend fun putLocation(lat: Double, lon: Double): Result =
        call(builder("/prefs/location").put(JSONObject().put("lat", lat).put("lon", lon).toString().toRequestBody(JSON)).build())

    /** A few seconds of a voice in a language, to hear a choice before saving it; null if the server could not make one. */
    class Preview(val audio: ByteArray, val backup: Boolean, val engine: String)

    /** `backup` is true when another engine spoke than the one chosen (Gemini out of quota, for instance). */
    suspend fun previewAudio(voice: String, lang: String): Preview? = withContext(Dispatchers.IO) {
        val v = java.net.URLEncoder.encode(voice, "UTF-8")
        runCatching {
            client.newCall(sign(builder("/prefs/preview?voice=$v&lang=$lang").get().build())).execute().use {
                val bytes = if (it.isSuccessful) it.body?.bytes()?.takeIf { b -> b.size > 500 } else null
                val engine = it.header("X-Voice-Engine").orEmpty()
                bytes?.let { b -> Preview(b, engine.isNotEmpty() && engine != it.header("X-Voice-Wanted"), engine) }
            }
        }.getOrNull()
    }

    /** The voices that can read a language (Gemini's, Microsoft's for that language, a custom provider's). */
    suspend fun getVoices(lang: String): Result = call(builder("/prefs/voices?lang=$lang").get().build())

    suspend fun markPlayed(id: Long): Result = call(builder("/briefing/$id/played").post("".toRequestBody(JSON)).build())

    suspend fun postHealth(body: JSONObject): Result =
        call(builder("/health/ingest").post(body.toString().toRequestBody(JSON)).build())

    /** A cheap authenticated call for the "test connection" button. */
    suspend fun check(): Result = call(builder("/inspect/timeline?limit=1").get().build())

    companion object {
        private val JSON = "application/json".toMediaType()
        private val client = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS).readTimeout(60, TimeUnit.SECONDS).writeTimeout(20, TimeUnit.SECONDS)
            .build()
    }
}
