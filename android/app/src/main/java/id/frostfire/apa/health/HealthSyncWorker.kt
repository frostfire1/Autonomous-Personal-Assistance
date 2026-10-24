package id.frostfire.apa.health

import android.content.Context
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.ActiveCaloriesBurnedRecord
import androidx.health.connect.client.records.DistanceRecord
import androidx.health.connect.client.records.RestingHeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import id.frostfire.apa.core.Api
import id.frostfire.apa.core.Config
import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.util.concurrent.TimeUnit

/**
 * Reads yesterday's and today's sleep, steps, calories, distance and resting heart rate from Health Connect (the
 * Huawei Health app writes there) and posts them to /health/ingest. Read-only; the server ignores repeats, so a
 * retry is always safe. Does nothing until the owner has granted the permissions once.
 */
class HealthSyncWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        val cfg = Config(applicationContext)
        if (!cfg.configured()) return Result.success()
        if (HealthConnectClient.getSdkStatus(applicationContext) != HealthConnectClient.SDK_AVAILABLE) return Result.success()
        val client = HealthConnectClient.getOrCreate(applicationContext)
        if (!client.permissionController.getGrantedPermissions().containsAll(PERMISSIONS)) return Result.success()
        return try {
            val zone = ZoneId.systemDefault()
            val today = LocalDate.now(zone)
            val metrics = (0..1).mapNotNull { back -> dayMetric(client, today.minusDays(back.toLong()), zone) }
            val sleep = sleeps(client, today, zone)
            if (metrics.isEmpty() && sleep.isEmpty()) return Result.success()
            val r = Api(cfg).postHealth(HealthPayload.build(cfg.deviceId, metrics, sleep))
            if (r.ok || r.code in 400..499 && r.code != 429) Result.success() else Result.retry()
        } catch (e: Exception) {
            Result.retry()
        }
    }

    private suspend fun dayMetric(client: HealthConnectClient, day: LocalDate, zone: ZoneId): Metric? {
        val start = day.atStartOfDay(zone).toInstant()
        val end = minOf(day.plusDays(1).atStartOfDay(zone).toInstant(), Instant.now())
        if (end <= start) return null
        val agg = client.aggregate(
            AggregateRequest(
                metrics = setOf(
                    StepsRecord.COUNT_TOTAL, ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL,
                    DistanceRecord.DISTANCE_TOTAL, RestingHeartRateRecord.BPM_AVG,
                ),
                timeRangeFilter = TimeRangeFilter.between(start, end),
            ),
        )
        return Metric(
            recordedAt = start,
            steps = agg[StepsRecord.COUNT_TOTAL],
            activeCalories = agg[ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL]?.inKilocalories?.toLong(),
            distanceMeters = agg[DistanceRecord.DISTANCE_TOTAL]?.inMeters,
            restingHeartRate = agg[RestingHeartRateRecord.BPM_AVG],
        )
    }

    private suspend fun sleeps(client: HealthConnectClient, today: LocalDate, zone: ZoneId): List<Sleep> {
        val from = today.minusDays(2).atStartOfDay(zone).toInstant()
        val sessions = client.readRecords(
            ReadRecordsRequest(SleepSessionRecord::class, TimeRangeFilter.between(from, Instant.now())),
        ).records
        // one entry per wake-up date: sessions that end the same day are summed (a night split by a bathroom visit)
        return sessions.groupBy { it.endTime.atZone(zone).toLocalDate() }.map { (date, list) ->
            val total = list.sumOf { Duration.between(it.startTime, it.endTime).toMinutes() }
            val stages = list.flatMap { it.stages }
            val asleep = stages.filter { it.stage != SleepSessionRecord.STAGE_TYPE_AWAKE }
            val deep = asleep.filter { it.stage == SleepSessionRecord.STAGE_TYPE_DEEP }
                .sumOf { Duration.between(it.startTime, it.endTime).toMinutes() }
            val asleepMins = asleep.sumOf { Duration.between(it.startTime, it.endTime).toMinutes() }
            Sleep(
                date = date,
                totalMinutes = if (asleepMins > 0) asleepMins else total,
                deepPct = if (asleepMins > 0) deep * 100.0 / asleepMins else null,
                awakeMinutes = if (stages.isNotEmpty()) total - asleepMins else null,
            )
        }
    }

    companion object {
        val PERMISSIONS = setOf(
            HealthPermission.getReadPermission(SleepSessionRecord::class),
            HealthPermission.getReadPermission(StepsRecord::class),
            HealthPermission.getReadPermission(ActiveCaloriesBurnedRecord::class),
            HealthPermission.getReadPermission(DistanceRecord::class),
            HealthPermission.getReadPermission(RestingHeartRateRecord::class),
        )

        fun schedulePeriodic(ctx: Context) {
            val req = PeriodicWorkRequestBuilder<HealthSyncWorker>(30, TimeUnit.MINUTES)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()).build()
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork("apa-health", ExistingPeriodicWorkPolicy.KEEP, req)
        }
    }
}
