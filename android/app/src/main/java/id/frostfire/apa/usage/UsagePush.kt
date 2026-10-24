package id.frostfire.apa.usage

import android.os.Build
import id.frostfire.apa.core.Api
import id.frostfire.apa.core.Config
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.util.concurrent.TimeUnit

/**
 * Sends how long each app was used today and yesterday to the server, read from the ActivityWatch app on this phone
 * (its local server on 127.0.0.1:5600). Only app names and seconds leave the phone: no window titles, nothing else.
 * Does nothing when ActivityWatch is not installed or not running, and at most once every 30 minutes.
 */
object UsagePush {
    private const val AW = "http://127.0.0.1:5600/api/0"
    private const val EVERY_MS = 30L * 60 * 1000
    private val http = OkHttpClient.Builder().connectTimeout(2, TimeUnit.SECONDS).readTimeout(15, TimeUnit.SECONDS).build()

    class Day(val apps: Map<String, Long>, val first: Pair<String, Instant>?, val last: Pair<String, Instant>?)

    private fun get(path: String, key: String): String? = runCatching {
        val req = Request.Builder().url("$AW$path").apply { if (key.isNotBlank()) header("Authorization", "Bearer $key") }.build()
        http.newCall(req).execute().use { if (it.isSuccessful) it.body?.string() else null }
    }.getOrNull()

    /** Seconds per app from the watcher's events of one day, plus the first and the last app used. Pure: tested without a phone. */
    fun summarize(events: JSONArray): Day {
        val totals = HashMap<String, Long>()
        var first: Pair<String, Instant>? = null
        var last: Pair<String, Instant>? = null
        for (i in 0 until events.length()) {
            val e = events.getJSONObject(i)
            val app = e.optJSONObject("data")?.optString("app").orEmpty().trim()
            val seconds = e.optDouble("duration", 0.0)
            if (app.isEmpty() || seconds < 1.0) continue
            val start = Instant.parse(e.getString("timestamp"))
            val end = start.plusMillis((seconds * 1000).toLong())
            totals[app] = (totals[app] ?: 0L) + seconds.toLong()
            if (first == null || start.isBefore(first.second)) first = app to start
            if (last == null || end.isAfter(last.second)) last = app to end
        }
        return Day(totals, first, last)
    }

    private fun payload(day: LocalDate, d: Day): JSONObject {
        val apps = JSONArray()
        d.apps.entries.sortedByDescending { it.value }.take(300).forEach { (app, secs) ->
            val o = JSONObject().put("app", app.take(120)).put("seconds", secs.coerceAtMost(86400))
            d.first?.takeIf { it.first == app }?.let { o.put("first_at", it.second.toString()) }
            d.last?.takeIf { it.first == app }?.let { o.put("last_at", it.second.toString()) }
            apps.put(o)
        }
        return JSONObject().put("device", Build.MODEL.take(40)).put("kind", "android").put("day", day.toString()).put("apps", apps)
    }

    suspend fun run(cfg: Config, api: Api) {
        val now = System.currentTimeMillis()
        if (now - cfg.usagePushedAt < EVERY_MS) return
        val buckets = get("/buckets/", cfg.awKey)?.let { JSONObject(it) } ?: return  // ActivityWatch is not running here
        val bucket = buckets.keys().asSequence().firstOrNull { it.startsWith("aw-watcher-android") } ?: return
        val zone = ZoneId.systemDefault()
        val today = LocalDate.now(zone)
        for (day in listOf(today.minusDays(1), today)) {
            val start = day.atStartOfDay(zone).toInstant()
            val end = minOf(day.plusDays(1).atStartOfDay(zone).toInstant(), Instant.ofEpochMilli(now))
            val body = get("/buckets/$bucket/events?start=$start&end=$end&limit=10000", cfg.awKey) ?: return
            val d = summarize(JSONArray(body))
            if (d.apps.isNotEmpty() && !api.putUsage(payload(day, d)).ok) return
        }
        cfg.usagePushedAt = now
    }
}
