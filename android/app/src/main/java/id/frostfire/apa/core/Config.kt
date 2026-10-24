package id.frostfire.apa.core

import android.content.Context
import java.time.LocalDate
import java.util.UUID

/** Settings the owner edits on the phone. The API key is stored encrypted with a hardware-backed key. */
class Config(context: Context) {
    private val p = context.applicationContext.getSharedPreferences("apa", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = p.getString("server_url", "")!!.trim().trimEnd('/')
        set(v) = p.edit().putString("server_url", v.trim().trimEnd('/')).apply()

    var apiKey: String
        get() = p.getString("api_key_enc", null)?.let { runCatching { KeystoreBox.decrypt(it) }.getOrNull() } ?: ""
        set(v) = p.edit().putString("api_key_enc", if (v.isBlank()) null else KeystoreBox.encrypt(v.trim())).apply()

    /** Optional: the server's INGEST_HMAC_SECRET. When set, every call is signed (the server then rejects unsigned ones). */
    var hmacSecret: String
        get() = p.getString("hmac_enc", null)?.let { runCatching { KeystoreBox.decrypt(it) }.getOrNull() } ?: ""
        set(v) = p.edit().putString("hmac_enc", if (v.isBlank()) null else KeystoreBox.encrypt(v.trim())).apply()

    /** Banking and wallet apps the owner picked on top of the built-in list, stored as "package|label". */
    var pickedApps: Set<String>
        get() = p.getStringSet("picked_apps", emptySet()) ?: emptySet()
        set(v) = p.edit().putStringSet("picked_apps", v).apply()

    val pickedPackages: Set<String> get() = pickedApps.map { it.substringBefore('|') }.toSet()

    /** The weather follows this phone's position (coarse). Off by default: nothing about location is sent until it is on. */
    var gpsWeather: Boolean
        get() = p.getBoolean("gps_weather", false)
        set(v) = p.edit().putBoolean("gps_weather", v).apply()

    /** How often the buffer is flushed as a safety net, in minutes (15 is Android's own minimum). */
    var syncMinutes: Int
        get() = p.getInt("sync_minutes", 15)
        set(v) = p.edit().putInt("sync_minutes", v).apply()

    /** Only flush on an unmetered network (Wi-Fi). The buffer simply waits, nothing is lost. */
    var syncWifiOnly: Boolean
        get() = p.getBoolean("sync_wifi_only", false)
        set(v) = p.edit().putBoolean("sync_wifi_only", v).apply()

    var wakeHour: Int
        get() = p.getInt("wake_hour", 6)
        set(v) = p.edit().putInt("wake_hour", v.coerceIn(0, 23)).apply()

    var wakeMinute: Int
        get() = p.getInt("wake_minute", 30)
        set(v) = p.edit().putInt("wake_minute", v.coerceIn(0, 59)).apply()

    /** Start the short briefing when the alarm is dismissed (seen through the notification listener). */
    var autoAfterAlarm: Boolean
        get() = p.getBoolean("auto_after_alarm", true)
        set(v) = p.edit().putBoolean("auto_after_alarm", v).apply()

    /** Also play at the configured time if the alarm could not be seen (some clock apps hide their notification). */
    var scheduledFallback: Boolean
        get() = p.getBoolean("scheduled_fallback", false)
        set(v) = p.edit().putBoolean("scheduled_fallback", v).apply()

    var onBluetooth: Boolean
        get() = p.getBoolean("on_bluetooth", false)
        set(v) = p.edit().putBoolean("on_bluetooth", v).apply()

    val deviceId: String
        get() = p.getString("device_id", null) ?: UUID.randomUUID().toString().also {
            p.edit().putString("device_id", it).apply()
        }

    fun configured(): Boolean = serverUrl.startsWith("http") && apiKey.isNotBlank()

    fun playedToday(): Boolean = p.getString("played_day", "") == LocalDate.now().toString()

    fun markPlayedNow() {
        p.edit().putString("played_day", LocalDate.now().toString()).putLong("played_at", System.currentTimeMillis()).apply()
    }

    /** Milliseconds since the last briefing finished, for the Bluetooth trigger's cool-down. */
    fun sincePlayedMs(): Long = System.currentTimeMillis() - p.getLong("played_at", 0L)
}
