package id.frostfire.apa.listener

import android.content.ComponentName
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.SystemClock
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
        if (runCatching { MediaCapture.handle(this, sbn) }.getOrDefault(false)) return  // a media player: remember what is playing
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

    override fun onListenerConnected() {
        super.onListenerConnected()
        Config(this).apply { listenerUp = true; listenerAt = System.currentTimeMillis() }
        registerNetworkWatch()
    }

    private var netCallback: ConnectivityManager.NetworkCallback? = null
    private var lastKick = 0L

    /** When any network (VPN included) comes up or becomes validated and rows are waiting, flush the queue at once. */
    private fun registerNetworkWatch() {
        runCatching {
            unregisterNetworkWatch()
            val cm = (getSystemService(CONNECTIVITY_SERVICE) as? ConnectivityManager) ?: return
            val request = NetworkRequest.Builder()
                .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                .removeCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)  // include VPN networks
                .build()
            val cb = object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) = kick()
                override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                    if (caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED) ||
                        caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) kick()
                }
            }
            cm.registerNetworkCallback(request, cb)
            netCallback = cb
        }
    }

    private fun unregisterNetworkWatch() {
        runCatching {
            val cb = netCallback ?: return
            netCallback = null
            (getSystemService(CONNECTIVITY_SERVICE) as? ConnectivityManager)?.unregisterNetworkCallback(cb)
        }
    }

    private fun kick() {
        runCatching {
            val now = SystemClock.elapsedRealtime()
            if (!shouldKick(now, lastKick, Outbox(this).pendingCount())) return
            lastKick = now
            SyncWorker.enqueue(this)
        }
    }

    override fun onDestroy() {
        unregisterNetworkWatch()
        super.onDestroy()
    }

    override fun onListenerDisconnected() {
        super.onListenerDisconnected()
        unregisterNetworkWatch()
        Config(this).apply { listenerUp = false; listenerAt = System.currentTimeMillis() }
        runCatching { requestRebind(ComponentName(this, BankNotificationListener::class.java)) }
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
        private const val KICK_GAP_MS = 20_000L

        /** Pure debounce decision: something is waiting and the last kick was at least 20 s ago. */
        internal fun shouldKick(now: Long, last: Long, pending: Int): Boolean =
            pending > 0 && (last == 0L || now - last >= KICK_GAP_MS)
    }
}
