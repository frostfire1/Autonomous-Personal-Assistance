package id.frostfire.apa.location

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.LocationManager
import androidx.core.content.ContextCompat
import id.frostfire.apa.core.Api
import id.frostfire.apa.core.Config

/** A position the phone already knows, with when it was taken. */
data class Fix(val lat: Double, val lon: Double, val timeMs: Long)

/** Chooses which known position is worth sending. Pure, so it is unit-tested. */
object LocationPick {
    /** Older than this and the owner may be somewhere else entirely: better no weather than the wrong city's. */
    const val MAX_AGE_MS = 6L * 60 * 60 * 1000

    fun best(fixes: List<Fix>, nowMs: Long): Fix? =
        fixes.filter { it.lat in -90.0..90.0 && it.lon in -180.0..180.0 && nowMs - it.timeMs in 0..MAX_AGE_MS }
            .maxByOrNull { it.timeMs }
}

/**
 * Sends the phone's coarse position to the server so the morning weather is for where the owner is. Only when the owner
 * switched "weather follows my GPS" on and allowed location. Uses the position the phone already has (no tracking, no
 * background location service); the server keeps it to about a kilometre and uses it for nothing but the weather.
 */
object LocationPush {
    fun allowed(ctx: Context): Boolean =
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED

    fun known(ctx: Context): List<Fix> {
        if (!allowed(ctx)) return emptyList()
        val lm = ctx.getSystemService(Context.LOCATION_SERVICE) as? LocationManager ?: return emptyList()
        return listOf(LocationManager.NETWORK_PROVIDER, LocationManager.PASSIVE_PROVIDER).mapNotNull { p ->
            try {
                lm.getLastKnownLocation(p)?.let { Fix(it.latitude, it.longitude, it.time) }
            } catch (e: SecurityException) {
                null
            } catch (e: IllegalArgumentException) {
                null
            }
        }
    }

    suspend fun push(ctx: Context): Boolean {
        val cfg = Config(ctx)
        if (!cfg.configured() || !cfg.gpsWeather) return false
        val fix = LocationPick.best(known(ctx), System.currentTimeMillis()) ?: return false
        return runCatching { Api(cfg).putLocation(fix.lat, fix.lon).ok }.getOrDefault(false)
    }
}
