package id.frostfire.apa.ambient

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import id.frostfire.apa.briefing.BriefingScheduler
import id.frostfire.apa.briefing.BriefingService
import id.frostfire.apa.core.Config
import id.frostfire.apa.health.HealthSyncWorker
import id.frostfire.apa.sync.SyncWorker
import java.util.concurrent.TimeUnit

/** The scheduled fallback fired: play the short briefing once a day, then schedule tomorrow's. */
class WakeAlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val cfg = Config(context)
        if (cfg.configured() && cfg.scheduledFallback && !cfg.playedToday()) {
            BriefingService.start(context, "short", "scheduled")
        }
        BriefingScheduler.schedule(context)
    }
}

/** A car or headset connected: play the short briefing, but not again within three hours of the last one. */
class BluetoothConnectedReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val cfg = Config(context)
        if (cfg.configured() && cfg.onBluetooth && cfg.sincePlayedMs() > TimeUnit.HOURS.toMillis(3)) {
            BriefingService.start(context, "short", "bluetooth")
        }
    }
}

/** After a reboot the schedules are gone: put them back. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        BriefingScheduler.schedule(context)
        SyncWorker.schedulePeriodic(context)
        HealthSyncWorker.schedulePeriodic(context)
    }
}
