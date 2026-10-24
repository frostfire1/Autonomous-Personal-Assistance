package id.frostfire.apa.listener

import android.os.Handler
import android.os.Looper
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import id.frostfire.apa.briefing.BriefingService
import id.frostfire.apa.core.Config
import id.frostfire.apa.data.Outbox
import id.frostfire.apa.sync.SyncWorker
import java.time.LocalDateTime

/**
 * Two jobs, one permission ("notification access", granted once in system settings):
 *  1. bank and e-wallet notifications are filtered, scrubbed of personal identifiers ON THE DEVICE, buffered
 *     and synced to the server;
 *  2. when the clock app's alarm notification disappears (the alarm was dismissed), the short briefing starts.
 */
class BankNotificationListener : NotificationListenerService() {
    private val handler = Handler(Looper.getMainLooper())

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val pkg = sbn.packageName
        val picked = Config(this).pickedPackages
        if (pkg !in NotificationFilter.PACKAGES && pkg !in picked) return
        val extras = sbn.notification.extras
        val text = (extras.getCharSequence("android.bigText") ?: extras.getCharSequence("android.text"))?.toString()
        if (!NotificationFilter.accept(pkg, text, picked)) return
        val clean = Scrubber.scrub(text!!).take(NotificationFilter.MAX_TEXT)
        val title = extras.getCharSequence("android.title")?.toString()?.let { Scrubber.scrub(it).take(200) }
        val key = IdempotencyKey.of(pkg, sbn.postTime, clean)
        if (Outbox(this).insert(key, pkg, title, clean, sbn.postTime)) SyncWorker.enqueue(this)
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification) {
        val pkg = sbn.packageName
        if (pkg !in AlarmDismissDetector.CLOCK_PACKAGES) return
        val cfg = Config(this)
        if (!cfg.autoAfterAlarm || !cfg.configured()) return
        val category = sbn.notification.category
        val ongoing = sbn.isOngoing
        // A snooze removes the ringing notification too and posts another one. Wait a moment and look: if the clock
        // app still has a notification up, the alarm was snoozed, not dismissed.
        handler.postDelayed({
            val stillThere = runCatching { activeNotifications.any { it.packageName in AlarmDismissDetector.CLOCK_PACKAGES } }
                .getOrDefault(false)
            if (!stillThere && AlarmDismissDetector.shouldPlay(pkg, category, ongoing, LocalDateTime.now(), cfg.playedToday())) {
                BriefingService.start(this, "short", "alarm")
            }
        }, SNOOZE_CHECK_MS)
    }

    companion object {
        private const val SNOOZE_CHECK_MS = 8_000L
    }
}
