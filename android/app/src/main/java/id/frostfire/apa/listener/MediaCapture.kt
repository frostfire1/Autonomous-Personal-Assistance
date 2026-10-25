package id.frostfire.apa.listener

import android.app.Notification
import android.content.Context
import android.service.notification.StatusBarNotification
import id.frostfire.apa.core.Api
import id.frostfire.apa.core.Config
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant

/**
 * Remembers what is playing (a video, a song) from the media notifications of YouTube, Spotify and the like, so the assistant can
 * answer "what did I last watch or listen to" and write it into the diary. Only the title, the artist or channel, the app's name
 * and the time are kept; the queue is sent with the next sync. Off with the switch in settings.
 */
object MediaCapture {
    private const val PREFS = "apa_media"
    private const val QUEUE = "queue"
    private const val MAX_QUEUE = 100
    private const val SAME_MS = 10 * 60 * 1000L

    /** Pure: is this notification a media player's, and what does it say? -> (title, artist) or null. */
    fun read(category: String?, hasSession: Boolean, title: CharSequence?, text: CharSequence?): Pair<String, String>? {
        if (category != Notification.CATEGORY_TRANSPORT && !hasSession) return null
        val t = title?.toString()?.trim().orEmpty()
        if (t.length < 2) return null
        return Scrubber.scrub(t).take(200) to Scrubber.scrub(text?.toString()?.trim().orEmpty()).take(120)
    }

    fun handle(ctx: Context, sbn: StatusBarNotification): Boolean {
        val cfg = Config(ctx)
        if (!cfg.mediaCapture || sbn.packageName == ctx.packageName) return false
        val n = sbn.notification
        val x = n.extras
        val hasSession = x.containsKey(Notification.EXTRA_MEDIA_SESSION)
        val found = read(n.category, hasSession, x.getCharSequence(Notification.EXTRA_TITLE), x.getCharSequence(Notification.EXTRA_TEXT)) ?: return false
        val app = runCatching { ctx.packageManager.getApplicationLabel(ctx.packageManager.getApplicationInfo(sbn.packageName, 0)).toString() }
            .getOrDefault(sbn.packageName)
        val sp = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val q = JSONArray(sp.getString(QUEUE, "[]"))
        val last = if (q.length() > 0) q.getJSONObject(q.length() - 1) else null
        if (last != null && last.optString("app") == app && last.optString("title") == found.first &&
            sbn.postTime - Instant.parse(last.getString("at")).toEpochMilli() < SAME_MS) return true  // the same one, updated again
        q.put(JSONObject().put("app", app).put("title", found.first).put("artist", found.second).put("at", Instant.ofEpochMilli(sbn.postTime).toString()))
        val trimmed = JSONArray()
        for (i in maxOf(0, q.length() - MAX_QUEUE) until q.length()) trimmed.put(q.get(i))
        sp.edit().putString(QUEUE, trimmed.toString()).apply()
        return true
    }

    /** Send what was queued; what the server did not accept stays for the next sync. */
    suspend fun flush(ctx: Context, api: Api) {
        val sp = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val q = JSONArray(sp.getString(QUEUE, "[]"))
        if (q.length() == 0) return
        val sent = q.length()
        val body = JSONObject().put("device", android.os.Build.MODEL.take(40)).put("items", q)
        if (api.postMedia(body).ok) {
            val rest = JSONArray(sp.getString(QUEUE, "[]"))  // anything added while sending stays
            val keep = JSONArray()
            for (i in sent until rest.length()) keep.put(rest.get(i))
            sp.edit().putString(QUEUE, keep.toString()).apply()
        }
    }
}
