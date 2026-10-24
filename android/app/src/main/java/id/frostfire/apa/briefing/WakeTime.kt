package id.frostfire.apa.briefing

import java.time.LocalDateTime

/** When the scheduled fallback briefing fires: the next occurrence of the configured time, strictly in the future. */
object WakeTime {
    fun next(now: LocalDateTime, hour: Int, minute: Int): LocalDateTime {
        require(hour in 0..23 && minute in 0..59) { "invalid time $hour:$minute" }
        val today = now.toLocalDate().atTime(hour, minute)
        return if (today.isAfter(now)) today else today.plusDays(1)
    }
}
