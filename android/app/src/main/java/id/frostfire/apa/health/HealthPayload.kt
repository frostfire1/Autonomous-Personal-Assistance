package id.frostfire.apa.health

import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.LocalDate

/** One reading, as the server's /health/ingest wants it. Fields the phone does not have are simply left out. */
data class Metric(
    val recordedAt: Instant,
    val steps: Long? = null,
    val restingHeartRate: Long? = null,
    val currentHeartRate: Long? = null,
    val activeCalories: Long? = null,
    val distanceMeters: Double? = null,
    val spo2: Double? = null,
)

data class Sleep(val date: LocalDate, val totalMinutes: Long, val deepPct: Double? = null, val awakeMinutes: Long? = null)

/** Builds the JSON body. Values outside what the server accepts are dropped here instead of failing the whole sync. */
object HealthPayload {
    fun build(deviceId: String, metrics: List<Metric>, sleep: List<Sleep>): JSONObject {
        val m = JSONArray()
        for (x in metrics.take(500)) {
            val o = JSONObject().put("recorded_at", x.recordedAt.toString())
            x.steps?.takeIf { it in 0..200_000 }?.let { o.put("step_count", it) }
            x.restingHeartRate?.takeIf { it in 25..220 }?.let { o.put("resting_heart_rate", it) }
            x.currentHeartRate?.takeIf { it in 25..250 }?.let { o.put("current_heart_rate", it) }
            x.activeCalories?.takeIf { it in 0..20_000 }?.let { o.put("active_calories", it) }
            x.distanceMeters?.takeIf { it in 0.0..300_000.0 }?.let { o.put("daily_distance_m", it) }
            x.spo2?.takeIf { it in 50.0..100.0 }?.let { o.put("spo2_pct", it) }
            if (o.length() > 1) m.put(o)  // a reading with nothing in it is not worth sending
        }
        val s = JSONArray()
        for (x in sleep.take(14)) {
            if (x.totalMinutes !in 0..1440) continue
            val o = JSONObject().put("sleep_date", x.date.toString()).put("total_sleep_mins", x.totalMinutes)
            x.deepPct?.takeIf { it in 0.0..100.0 }?.let { o.put("deep_sleep_pct", it) }
            x.awakeMinutes?.takeIf { it in 0..1440 }?.let { o.put("awake_mins", it) }
            s.put(o)
        }
        return JSONObject().put("device_id", deviceId).put("metrics", m).put("sleep", s)
    }
}
