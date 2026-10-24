package id.frostfire.apa.usage

import android.app.AppOpsManager
import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.Context
import android.os.Process
import java.time.Instant

/**
 * App usage straight from Android (Settings > Special access > Usage access), with no other app needed. Used when the owner
 * has granted that access; ActivityWatch is the fallback. Only app names and seconds are read, and nothing else is stored.
 */
object UsageStatsSource {
    fun granted(ctx: Context): Boolean {
        val ops = ctx.getSystemService(Context.APP_OPS_SERVICE) as AppOpsManager
        @Suppress("DEPRECATION")
        return ops.checkOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), ctx.packageName) == AppOpsManager.MODE_ALLOWED
    }

    private fun label(ctx: Context, pkg: String): String = runCatching {
        ctx.packageManager.getApplicationLabel(ctx.packageManager.getApplicationInfo(pkg, 0)).toString()
    }.getOrDefault(pkg)

    /** Time in the foreground per app between `start` and `end`; a session ends when the app leaves the screen or the screen goes off. */
    fun day(ctx: Context, start: Instant, end: Instant): UsagePush.Day {
        val mgr = ctx.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
        val events = mgr.queryEvents(start.toEpochMilli(), end.toEpochMilli())
        val home = ctx.packageManager.resolveActivity(android.content.Intent(android.content.Intent.ACTION_MAIN).addCategory(android.content.Intent.CATEGORY_HOME), 0)
            ?.activityInfo?.packageName  // the launcher is the screen between apps, not an app the owner used
        val open = HashMap<String, Long>()
        val totals = HashMap<String, Long>()
        var first: Pair<String, Instant>? = null
        var last: Pair<String, Instant>? = null
        fun close(pkg: String, at: Long) {
            val from = open.remove(pkg) ?: return
            val secs = (at - from) / 1000
            if (secs < 1 || pkg == ctx.packageName || pkg == home) return
            val name = label(ctx, pkg)
            totals[name] = (totals[name] ?: 0L) + secs
            val s = Instant.ofEpochMilli(from)
            val e = Instant.ofEpochMilli(at)
            if (first == null || s.isBefore(first!!.second)) first = name to s
            if (last == null || e.isAfter(last!!.second)) last = name to e
        }
        val e = UsageEvents.Event()
        while (events.hasNextEvent()) {
            events.getNextEvent(e)
            @Suppress("DEPRECATION")
            when (e.eventType) {
                UsageEvents.Event.MOVE_TO_FOREGROUND -> open.putIfAbsent(e.packageName, e.timeStamp)
                UsageEvents.Event.MOVE_TO_BACKGROUND -> close(e.packageName, e.timeStamp)
                UsageEvents.Event.SCREEN_NON_INTERACTIVE -> open.keys.toList().forEach { close(it, e.timeStamp) }
            }
        }
        open.keys.toList().forEach { close(it, end.toEpochMilli()) }  // still in use right now
        return UsagePush.Day(totals, first, last)
    }
}
