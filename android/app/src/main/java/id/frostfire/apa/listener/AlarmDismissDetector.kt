package id.frostfire.apa.listener

import java.time.LocalDateTime

/**
 * "A short briefing after the alarm" without an exact-alarm guess: when the clock app's ringing notification is
 * REMOVED, the alarm was dismissed (or snoozed, which the clock app signals with a new notification). Android has
 * no reliable ALARM_DISMISSED broadcast for other apps, but the notification listener already runs.
 *
 * Pure logic: the caller passes what it saw, so this is testable without Android.
 */
object AlarmDismissDetector {
    val CLOCK_PACKAGES = setOf(
        "com.google.android.deskclock", "com.android.deskclock", "com.huawei.deskclock",
        "com.huawei.android.deskclock", "com.sec.android.app.clockpackage", "com.coloros.alarmclock",
        "com.oneplus.deskclock", "com.miui.clock",
    )
    const val MORNING_FROM = 4  // hour of day, inclusive
    const val MORNING_UNTIL = 11  // exclusive

    /** True when a removed notification is a dismissed morning alarm and today's briefing has not played yet. */
    fun shouldPlay(
        packageName: String,
        category: String?,
        isOngoing: Boolean,
        now: LocalDateTime,
        playedToday: Boolean,
    ): Boolean {
        if (packageName !in CLOCK_PACKAGES || playedToday) return false
        val isAlarm = category == "alarm" || isOngoing  // the ringing notification is an ongoing "alarm" one
        return isAlarm && now.hour in MORNING_FROM until MORNING_UNTIL
    }
}
