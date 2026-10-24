package id.frostfire.apa.sync

import android.content.ComponentName
import android.content.Context
import android.service.notification.NotificationListenerService
import androidx.core.app.NotificationManagerCompat
import id.frostfire.apa.listener.BankNotificationListener
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import id.frostfire.apa.core.Api
import id.frostfire.apa.core.Config
import id.frostfire.apa.data.Outbox
import java.util.concurrent.TimeUnit

/** Sends buffered notifications to POST /ingest/notification. Safe to run repeatedly: the key makes resends harmless. */
class SyncWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        val cfg = Config(applicationContext)
        if (!cfg.configured()) return Result.success()  // nothing to send to yet; the rows wait in the buffer
        val outbox = Outbox(applicationContext)
        val api = Api(cfg)
        runCatching { healListener(cfg) }
        runCatching {
            val version = runCatching {
                applicationContext.packageManager.getPackageInfo(applicationContext.packageName, 0).versionName
            }.getOrNull() ?: "0.2.0"
            api.heartbeat(cfg.listenerUp, outbox.pendingCount(), version)
        }
        for (row in outbox.pending()) {
            val r = try {
                api.postNotification(row.key, row.pkg, row.title, row.text, row.postedAt)
            } catch (e: Exception) {
                enqueueRetry(applicationContext)  // server unreachable (VPN down?): fixed short delay, not exponential backoff
                return Result.success()
            }
            when {
                r.ok -> outbox.markSent(row.id)
                r.code in 400..499 && r.code != 429 -> outbox.markSent(row.id)  // the server refused it for good (bad data / key)
                else -> {
                    enqueueRetry(applicationContext)
                    return Result.success()
                }
            }
        }
        outbox.purgeSentOlderThan(TimeUnit.DAYS.toMillis(7))
        return Result.success()
    }

    /** Access is granted but the OS has no live connection (or no record for a day): ask it to bind the service again. */
    private fun healListener(cfg: Config) {
        val granted = NotificationManagerCompat.getEnabledListenerPackages(applicationContext).contains(applicationContext.packageName)
        if (!granted) return
        val stale = System.currentTimeMillis() - cfg.listenerAt > TimeUnit.HOURS.toMillis(24)
        if (!cfg.listenerUp || stale) {
            NotificationListenerService.requestRebind(ComponentName(applicationContext, BankNotificationListener::class.java))
        }
    }

    companion object {
        private const val NOW = "apa-sync-now"
        private const val RETRY = "apa-sync-retry"
        private const val PERIODIC = "apa-sync-periodic"
        private fun online(ctx: Context): Constraints = Constraints.Builder()
            .setRequiredNetworkType(if (Config(ctx).syncWifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED).build()

        fun enqueue(ctx: Context) {
            val req = OneTimeWorkRequestBuilder<SyncWorker>()
                .setConstraints(online(ctx)).setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build()
            WorkManager.getInstance(ctx).enqueueUniqueWork(NOW, ExistingWorkPolicy.APPEND_OR_REPLACE, req)
        }

        /** One-time retry after a fixed delay (replaces any pending retry). */
        fun enqueueRetry(ctx: Context, minutes: Long = 5) {
            val req = OneTimeWorkRequestBuilder<SyncWorker>()
                .setConstraints(online(ctx)).setInitialDelay(minutes, TimeUnit.MINUTES).build()
            WorkManager.getInstance(ctx).enqueueUniqueWork(RETRY, ExistingWorkPolicy.REPLACE, req)
        }

        /** A safety net: even if a trigger is missed, the buffer is flushed on the owner's chosen interval while online. */
        fun schedulePeriodic(ctx: Context) {
            val minutes = id.frostfire.apa.background.BackgroundHelp.clampSync(Config(ctx).syncMinutes).toLong()
            val req = PeriodicWorkRequestBuilder<SyncWorker>(minutes, TimeUnit.MINUTES).setConstraints(online(ctx)).build()
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.UPDATE, req)
        }
    }
}
