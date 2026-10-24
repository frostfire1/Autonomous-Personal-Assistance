package id.frostfire.apa.briefing

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import id.frostfire.apa.ambient.WakeAlarmReceiver
import id.frostfire.apa.core.Config
import java.time.LocalDateTime
import java.time.ZoneId

/**
 * The fallback trigger: a scheduled time for phones whose clock app does not show a notification the app can see.
 * An inexact alarm is enough (a few minutes either way is fine for a briefing) and needs no special permission.
 */
object BriefingScheduler {
    private fun pending(ctx: Context): PendingIntent = PendingIntent.getBroadcast(
        ctx, 7, Intent(ctx, WakeAlarmReceiver::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
    )

    fun schedule(ctx: Context) {
        val am = ctx.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val cfg = Config(ctx)
        if (!cfg.scheduledFallback) {
            am.cancel(pending(ctx))
            return
        }
        val at = WakeTime.next(LocalDateTime.now(), cfg.wakeHour, cfg.wakeMinute)
        am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at.atZone(ZoneId.systemDefault()).toInstant().toEpochMilli(), pending(ctx))
    }
}
