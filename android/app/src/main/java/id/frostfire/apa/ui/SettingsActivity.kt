package id.frostfire.apa.ui

import android.Manifest
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.content.ComponentName
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import android.net.Uri
import android.os.PowerManager
import android.service.notification.NotificationListenerService
import android.widget.ArrayAdapter
import id.frostfire.apa.background.BackgroundHelp
import id.frostfire.apa.listener.BankNotificationListener
import android.widget.ScrollView
import android.widget.Spinner
import android.media.MediaPlayer
import android.widget.AdapterView
import id.frostfire.apa.location.LocationPush
import org.json.JSONObject
import java.io.File
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import android.app.AlertDialog
import androidx.core.app.NotificationManagerCompat
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.PermissionController
import androidx.lifecycle.lifecycleScope
import id.frostfire.apa.briefing.BriefingScheduler
import id.frostfire.apa.briefing.BriefingService
import id.frostfire.apa.core.Api
import id.frostfire.apa.core.Config
import id.frostfire.apa.data.Outbox
import id.frostfire.apa.health.HealthSyncWorker
import id.frostfire.apa.listener.BankApps
import id.frostfire.apa.sync.SyncWorker
import kotlinx.coroutines.launch

/** One plain screen: where the server is, the key, when to brief, and the permissions. No account, no sign-in. */
class SettingsActivity : ComponentActivity() {
    private lateinit var cfg: Config
    private lateinit var status: TextView

    private var langKeys = listOf<String>()
    private var voiceKeys = listOf<String>()
    private var topicKeys = listOf<String>()
    private var topicLabels = listOf<String>()
    private var topicOn = booleanArrayOf()
    private lateinit var keywordsField: EditText
    private lateinit var topicsText: TextView
    private var prefsLoaded = false
    private var langReady = false
    private var player: MediaPlayer? = null
    private lateinit var langSpin: Spinner
    private lateinit var voiceSpin: Spinner
    private lateinit var currencySpin: Spinner
    private lateinit var currencyLabel: TextView
    private var currencyKeys = listOf<String>()
    private lateinit var gpsBox: CheckBox
    private lateinit var cityField: EditText
    private lateinit var bgStatus: TextView
    private lateinit var serverStatus: TextView
    private lateinit var prefsStatus: TextView

    private companion object {
        const val DEFAULT_SERVER = "http://192.168.1.10:8000"  // an example address, replace it with your own server
    }
    private lateinit var syncSpin: Spinner
    private lateinit var wifiBox: CheckBox

    private val askLocation = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (!granted) {
            cfg.gpsWeather = false
            gpsBox.isChecked = false
            status.text = "Location permission denied, so weather uses the city you typed."
        } else {
            lifecycleScope.launch { LocationPush.push(this@SettingsActivity) }
        }
        refresh()
    }

    private val askNotifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) { refresh() }
    private val askHealth = registerForActivityResult(PermissionController.createRequestPermissionResultContract()) {
        HealthSyncWorker.schedulePeriodic(this)
        refresh()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        cfg = Config(this)
        val pad = (16 * resources.displayMetrics.density).toInt()
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad) }

        fun label(t: String) = TextView(this).apply { text = t; setPadding(0, pad / 2, 0, 0) }.also(col::addView)
        fun field(value: String, hint: String) = EditText(this).apply { setText(value); this.hint = hint; setSingleLine() }.also(col::addView)
        fun box(t: String, on: Boolean) = CheckBox(this).apply { text = t; isChecked = on }.also(col::addView)
        fun button(t: String, f: () -> Unit) = Button(this).apply { text = t; setOnClickListener { f() } }.also(col::addView)

        TextView(this).apply { text = "APA: Settings"; textSize = 22f; setPadding(0, 0, 0, pad / 2) }.also(col::addView)
        serverStatus = TextView(this).also(col::addView)
        label("1. SERVER ADDRESS (host:port), at home or over WireGuard")
        val url = field(cfg.serverUrl.ifBlank { DEFAULT_SERVER }, DEFAULT_SERVER)  // real text, not a faint hint: it looks like an input
        label("API key")
        val key = field("", if (cfg.apiKey.isBlank()) "paste key" else "saved (encrypted); fill in to replace")
        label("Signing secret (optional, same as INGEST_HMAC_SECRET on the server)")
        val hmac = field("", if (cfg.hmacSecret.isBlank()) "empty = not signed" else "saved (encrypted); fill in to replace")
        label(if (id.frostfire.apa.usage.UsageStatsSource.granted(this)) "App usage: allowed (sent with each sync, app names and minutes only)"
              else "App usage: not allowed yet. Tap the button, find APA in the list and allow it.")
        val media = box("Remember what I watch and listen to (titles from media notifications, sent to your own server)", cfg.mediaCapture)
        button("Allow app usage access") { startActivity(android.content.Intent(android.provider.Settings.ACTION_USAGE_ACCESS_SETTINGS)) }
        label("ActivityWatch API key (optional: from the ActivityWatch app on this phone, to send your app usage)")
        val aw = field("", if (cfg.awKey.isBlank()) "empty = no app usage sent" else "saved (encrypted); fill in to replace")
        button("Save server & test connection") {
            cfg.serverUrl = url.text.toString()
            if (key.text.isNotBlank()) cfg.apiKey = key.text.toString()
            if (hmac.text.isNotBlank()) cfg.hmacSecret = hmac.text.toString()
            if (aw.text.isNotBlank()) cfg.awKey = aw.text.toString()
            key.text.clear(); hmac.text.clear(); aw.text.clear()
            SyncWorker.schedulePeriodic(this); SyncWorker.enqueue(this)
            testConnection()
        }
        label("Wake time (HH:MM) for the scheduled fallback")
        val time = field("%02d:%02d".format(cfg.wakeHour, cfg.wakeMinute), "06:30")
        val alarm = box("Play a short briefing after the alarm is dismissed", cfg.autoAfterAlarm)
        val sched = box("Fallback: play at the time above if no alarm is detected", cfg.scheduledFallback)
        val bt = box("Play when car/headset Bluetooth connects", cfg.onBluetooth)

        button("Save") {
            cfg.serverUrl = url.text.toString()
            if (key.text.isNotBlank()) cfg.apiKey = key.text.toString()
            if (hmac.text.isNotBlank()) cfg.hmacSecret = hmac.text.toString()
            if (aw.text.isNotBlank()) cfg.awKey = aw.text.toString()
            Regex("""(\d{1,2}):(\d{2})""").matchEntire(time.text.toString().trim())?.let {
                cfg.wakeHour = it.groupValues[1].toInt(); cfg.wakeMinute = it.groupValues[2].toInt()
            }
            cfg.autoAfterAlarm = alarm.isChecked; cfg.scheduledFallback = sched.isChecked; cfg.onBluetooth = bt.isChecked
            cfg.mediaCapture = media.isChecked
            key.text.clear()
            BriefingScheduler.schedule(this)
            SyncWorker.schedulePeriodic(this)
            SyncWorker.enqueue(this)
            refresh()
        }
        button("Test connection") { testConnection() }
        button("1. Allow notification access (bank & alarm)") {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }
        button("Choose bank & wallet apps to monitor") { pickBankApps() }
        button("2. Allow app notifications") {
            if (android.os.Build.VERSION.SDK_INT >= 33) askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        button("3. Allow Health Connect (sleep, steps)") {
            if (HealthConnectClient.getSdkStatus(this) == HealthConnectClient.SDK_AVAILABLE) {
                askHealth.launch(HealthSyncWorker.PERMISSIONS)
            } else {
                status.text = "Health Connect is not installed on this phone."
            }
        }
        button("Play short briefing now") { BriefingService.start(this, "short", "manual") }
        button("Play full briefing") { BriefingService.start(this, "full", "manual") }
        label("Background: keep the app running when closed")
        bgStatus = TextView(this).also(col::addView)
        button("Send queued notifications now") { sendQueuedNow() }
        button("Turn off battery saving for APA") { requestBatteryExemption() }
        button("Open autostart settings (${android.os.Build.MANUFACTURER})") { openVendorSettings() }
        button("Re-link notification listener") { rebindListener() }
        label("Resend queue every (minutes), a safety net if a trigger is missed")
        syncSpin = Spinner(this).also(col::addView)
        syncSpin.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, BackgroundHelp.SYNC_CHOICES.map { "$it min" })
        syncSpin.setSelection(BackgroundHelp.SYNC_CHOICES.indexOf(BackgroundHelp.clampSync(cfg.syncMinutes)))
        syncSpin.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(p: AdapterView<*>?, v: View?, pos: Int, id: Long) {
                val minutes = BackgroundHelp.SYNC_CHOICES[pos]
                if (minutes != cfg.syncMinutes) { cfg.syncMinutes = minutes; SyncWorker.schedulePeriodic(this@SettingsActivity) }
            }
            override fun onNothingSelected(p: AdapterView<*>?) = Unit
        }
        wifiBox = box("Send queue over Wi-Fi only (items wait, nothing is lost)", cfg.syncWifiOnly)
        wifiBox.setOnCheckedChangeListener { _, on -> cfg.syncWifiOnly = on; SyncWorker.schedulePeriodic(this) }

        label("Morning briefing: language, voice and weather")
        prefsStatus = TextView(this).apply { text = "Loading options from the server..." }.also(col::addView)
        langSpin = Spinner(this).also(col::addView)
        langSpin.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(p: AdapterView<*>?, v: View?, pos: Int, id: Long) {
                if (langReady && pos in langKeys.indices) reloadVoices(langKeys[pos])
            }
            override fun onNothingSelected(p: AdapterView<*>?) = Unit
        }
        voiceSpin = Spinner(this).also(col::addView)
        currencyLabel = label("Currency for amounts in the briefing").also { it.visibility = View.GONE }
        currencySpin = Spinner(this).also(col::addView)
        currencySpin.visibility = View.GONE
        label("News topics and followed keywords")
        topicsText = TextView(this).also(col::addView)
        button("Choose news topics") { pickTopics() }
        keywordsField = field("", "Keywords to follow, comma-separated, e.g. bitcoin, kubernetes")
        gpsBox = box("Weather follows phone GPS location (coarse, about 1 km, sent when the app is opened)", cfg.gpsWeather)
        cityField = field("", "City for weather when GPS is off, e.g. Surabaya")
        gpsBox.setOnCheckedChangeListener { _, on ->
            if (on && !LocationPush.allowed(this)) askLocation.launch(Manifest.permission.ACCESS_COARSE_LOCATION)
            cityField.isEnabled = !on
        }
        button("Play voice sample") { playSample() }
        button("Save briefing options") { savePrefs() }
        status = TextView(this).apply { setPadding(0, pad, 0, 0) }.also(col::addView)
        val scroll = ScrollView(this).apply { clipToPadding = false; addView(col, LinearLayout.LayoutParams(-1, -2)) }
        ViewCompat.setOnApplyWindowInsetsListener(scroll) { v, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout() or WindowInsetsCompat.Type.ime(),
            )
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        setContentView(scroll)
        refresh()
        loadPrefs()
    }

    /** Asks the server something cheap with the saved address and key; the answer shows under the server box. */
    private fun testConnection() {
        serverStatus.text = "Testing connection..."
        lifecycleScope.launch {
            val answer = try {
                val r = Api(cfg).check()
                if (r.ok) "✓ Connected to ${cfg.serverUrl}" else "✗ Server replied ${r.code} (wrong key?)"
            } catch (e: Exception) {
                "✗ Could not reach ${cfg.serverUrl.ifBlank { "the server (no address entered)" }}"
            }
            serverStatus.text = answer
            status.text = answer
        }
    }

    /** The checklist of what the phone must allow, with the reason for each cross. */
    private fun backgroundChecks(): List<BackgroundHelp.Check> {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        val listener = NotificationManagerCompat.getEnabledListenerPackages(this).contains(packageName)
        val notify = NotificationManagerCompat.from(this).areNotificationsEnabled()
        val waiting = Outbox(this).pendingCount()
        val queue = if (waiting > 0) listOf(
            BackgroundHelp.Check("$waiting notifications waiting to be sent", false,
                "the server could not be reached: connect the VPN (Tailscale) or the home Wi-Fi; they are sent automatically"),
        ) else emptyList()
        return queue + listOf(
            BackgroundHelp.Check("Notification access (reads bank notifications)", listener, "without this, bank notifications are not read"),
            BackgroundHelp.Check("Notification listener connected", cfg.listenerUp,
                "Android stopped the app: turn Autostart on and press Re-link notification listener"),
            BackgroundHelp.Check("App notifications allowed", notify, "the morning briefing cannot show in the notification bar"),
            BackgroundHelp.Check("Battery saving turned off for APA", pm.isIgnoringBatteryOptimizations(packageName),
                "the phone may freeze the app and the queue will not be sent"),
        )
    }

    private fun sendQueuedNow() {
        val before = Outbox(this).pendingCount()
        SyncWorker.enqueue(this)
        status.text = "$before waiting. Sending..."
        lifecycleScope.launch {
            kotlinx.coroutines.delay(6000)
            val left = Outbox(this@SettingsActivity).pendingCount()
            status.text = "$before were waiting, $left remain."
            refresh()
            status.text = "$before were waiting, $left remain.\n" + status.text
        }
    }

    private fun requestBatteryExemption() {
        try {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        } catch (e: Exception) {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }

    /** The maker's autostart screen if it can be opened; otherwise this app's own system page, with the switch named. */
    private fun openVendorSettings() {
        for (t in BackgroundHelp.targetsFor(android.os.Build.MANUFACTURER)) {
            try {
                startActivity(Intent().setComponent(ComponentName(t.pkg, t.cls)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                status.text = "Look for: ${BackgroundHelp.vendorSwitchName(android.os.Build.MANUFACTURER)}"
                return
            } catch (e: Exception) {
                continue
            }
        }
        startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
        status.text = "Manufacturer screen not found. Here, look for: ${BackgroundHelp.vendorSwitchName(android.os.Build.MANUFACTURER)}"
    }

    private fun rebindListener() {
        NotificationListenerService.requestRebind(ComponentName(this, BankNotificationListener::class.java))
        status.text = "Asked the system to reconnect the notification listener."
    }

    /** Fills the language and voice lists from the server and shows what is saved. */
    private fun loadPrefs() {
        if (!cfg.configured()) {
            prefsStatus.text = "Enter the server address and key above first, then these options will appear."
            return
        }
        lifecycleScope.launch {
            try {
                val r = Api(cfg).getPrefs()
                if (!r.ok) {
                    prefsStatus.text = when (r.code) {
                        404 -> "These options are not available yet: the server needs updating to the latest version."
                        401 -> "The server rejected the API key. Check the key above."
                        else -> "Server replied ${r.code}; options could not be loaded."
                    }
                    return@launch
                }
                val j = JSONObject(r.body)
                val langs = j.getJSONArray("languages")
                val voices = j.getJSONArray("voices")
                langKeys = (0 until langs.length()).map { langs.getJSONObject(it).getString("key") }
                voiceKeys = (0 until voices.length()).map { voices.getJSONObject(it).getString("key") }
                langSpin.adapter = ArrayAdapter(this@SettingsActivity, android.R.layout.simple_spinner_dropdown_item,
                    (0 until langs.length()).map { langs.getJSONObject(it).getString("label") })
                voiceSpin.adapter = ArrayAdapter(this@SettingsActivity, android.R.layout.simple_spinner_dropdown_item,
                    (0 until voices.length()).map { voices.getJSONObject(it).getString("label") })
                val p = j.getJSONObject("prefs")
                val saved = p.getString("voice")
                if (saved !in voiceKeys) {  // a short legacy key (zephyr) is not in the "engine:name" list: keep it selectable
                    voiceKeys = listOf(saved) + voiceKeys
                    voiceSpin.adapter = ArrayAdapter(this@SettingsActivity, android.R.layout.simple_spinner_dropdown_item,
                        listOf(saved) + (0 until voices.length()).map { voices.getJSONObject(it).getString("label") })
                }
                langSpin.setSelection(langKeys.indexOf(p.getString("language")).coerceAtLeast(0))
                voiceSpin.setSelection(voiceKeys.indexOf(saved).coerceAtLeast(0))
                val curs = j.optJSONArray("currencies")
                if (curs != null && curs.length() > 0) {
                    currencyKeys = (0 until curs.length()).map { curs.getJSONObject(it).getString("key") }
                    currencySpin.adapter = ArrayAdapter(this@SettingsActivity, android.R.layout.simple_spinner_dropdown_item,
                        (0 until curs.length()).map { curs.getJSONObject(it).getString("label") })
                    currencySpin.setSelection(currencyKeys.indexOf(p.optString("currency", "IDR")).coerceAtLeast(0))
                    currencyLabel.visibility = View.VISIBLE
                    currencySpin.visibility = View.VISIBLE
                } else {
                    currencyKeys = emptyList()
                    currencyLabel.visibility = View.GONE
                    currencySpin.visibility = View.GONE
                }
                val tops = j.optJSONArray("topics")
                if (tops != null) {
                    topicKeys = (0 until tops.length()).map { tops.getJSONObject(it).getString("key") }
                    topicLabels = (0 until tops.length()).map { tops.getJSONObject(it).getString("label") }
                    val on = p.optJSONArray("topics")?.let { a -> (0 until a.length()).map { a.getString(it) } } ?: emptyList()
                    topicOn = BooleanArray(topicKeys.size) { topicKeys[it] in on }
                    showTopics()
                }
                keywordsField.setText(p.optJSONArray("keywords")?.let { a -> (0 until a.length()).joinToString(", ") { a.getString(it) } } ?: "")
                langReady = true
                cityField.setText(p.optString("city", ""))
                gpsBox.isChecked = p.optBoolean("gps", false)
                cityField.isEnabled = !gpsBox.isChecked
                prefsLoaded = true
                prefsStatus.text = "Options are saved on the server, shared by the app and the web."
            } catch (e: Exception) {
                prefsStatus.text = "Could not reach the server to load options."
            }
        }
    }

    /** The voices of the newly chosen language replace the list (the choice is saved with the other options). */
    private fun reloadVoices(lang: String) {
        lifecycleScope.launch {
            val r = try { Api(cfg).getVoices(lang) } catch (e: Exception) { return@launch }
            if (!r.ok) return@launch
            val arr = JSONObject(r.body).getJSONArray("voices")
            voiceKeys = (0 until arr.length()).map { arr.getJSONObject(it).getString("key") }
            voiceSpin.adapter = ArrayAdapter(this@SettingsActivity, android.R.layout.simple_spinner_dropdown_item,
                (0 until arr.length()).map { arr.getJSONObject(it).getString("label") })
        }
    }

    private fun showTopics() {
        val names = topicKeys.indices.filter { topicOn[it] }.map { topicLabels[it] }
        topicsText.text = if (names.isEmpty()) "No topics selected." else names.joinToString(", ")
    }

    private fun pickTopics() {
        if (topicKeys.isEmpty()) { status.text = "Options have not been loaded from the server."; return }
        val draft = topicOn.copyOf()
        android.app.AlertDialog.Builder(this).setTitle("News topics")
            .setMultiChoiceItems(topicLabels.toTypedArray(), draft) { _, i, on -> draft[i] = on }
            .setPositiveButton("Apply") { _, _ -> topicOn = draft; showTopics() }
            .setNegativeButton("Cancel", null).show()
    }

    private fun savePrefs() {
        if (!prefsLoaded) { status.text = "Options have not been loaded from the server."; return }
        val gps = gpsBox.isChecked
        val body = JSONObject().put("language", langKeys[langSpin.selectedItemPosition])
            .put("voice", voiceKeys[voiceSpin.selectedItemPosition]).put("gps", gps)
            .put("topics", org.json.JSONArray(topicKeys.filterIndexed { i, _ -> topicOn.getOrElse(i) { false } }))
            .put("keywords", org.json.JSONArray(keywordsField.text.split(",").map { it.trim() }.filter { it.isNotEmpty() }))
        if (currencyKeys.isNotEmpty() && currencySpin.selectedItemPosition in currencyKeys.indices) {
            body.put("currency", currencyKeys[currencySpin.selectedItemPosition])
        }
        if (!gps && cityField.text.isNotBlank()) body.put("city", cityField.text.toString().trim())
        cfg.gpsWeather = gps
        lifecycleScope.launch {
            status.text = try {
                val r = Api(cfg).putPrefs(body)
                if (gps) LocationPush.push(this@SettingsActivity)
                when {
                    r.ok -> "Saved. Applies from the next briefing."
                    r.code == 422 -> "City not found or option not recognised."
                    else -> "Server replied ${r.code}."
                }
            } catch (e: Exception) {
                "Could not reach the server."
            }
        }
    }

    private fun playSample() {
        if (!prefsLoaded) { status.text = "Options have not been loaded from the server."; return }
        val voice = voiceKeys[voiceSpin.selectedItemPosition]
        val lang = langKeys[langSpin.selectedItemPosition]
        lifecycleScope.launch {
            status.text = "Creating voice sample..."
            val preview = try { Api(cfg).previewAudio(voice, lang) } catch (e: Exception) { null }
            if (preview == null) { status.text = "Could not create the voice sample yet."; return@launch }
            val f = File(cacheDir, "sample.audio").also { it.writeBytes(preview.audio) }
            player?.release()
            player = MediaPlayer().apply {
                setDataSource(f.absolutePath)
                setOnCompletionListener { status.text = "" }
                prepare(); start()
            }
            status.text = if (preview.backup) "This is a backup voice (${preview.engine}): your chosen voice is unavailable, usually because the free quota ran out. Try again later." else ""
        }
    }

    override fun onDestroy() {
        player?.release()
        super.onDestroy()
    }

    /** Lists the banking and wallet apps found on this phone; the ticked ones are listened to (the rest are ignored). */
    private fun pickBankApps(showAll: Boolean = false, keep: Set<String>? = null) {
        val found = if (showAll) BankApps.scanAll(this) else BankApps.scan(this)
        if (found.isEmpty()) {
            status.text = "No known bank apps found on this phone."
            return
        }
        val saved = cfg.pickedPackages
        val ticked = BooleanArray(found.size) {
            val p = found[it]
            when {
                keep != null -> p.builtIn || p.packageName in keep
                showAll -> p.builtIn || p.packageName in saved || (saved.isEmpty() && BankApps.looksLikeBank(p.label))
                else -> p.builtIn || p.packageName in saved || saved.isEmpty()
            }
        }
        val builder = AlertDialog.Builder(this)
            .setTitle("Apps whose notifications are read")
            .setMultiChoiceItems(found.map { it.label + if (it.builtIn) "" else "  (new)" }.toTypedArray(), ticked) { _, i, on -> ticked[i] = on }
            .setPositiveButton("Save") { _, _ ->
                val chosen = found.filterIndexed { i, _ -> ticked[i] }.filter { !it.builtIn }
                cfg.pickedApps = chosen.map { "${it.packageName}|${it.label}" }.toSet()
                lifecycleScope.launch {
                    status.text = try {
                        val r = Api(cfg).postPackages(chosen.map { it.packageName to it.label })
                        if (r.ok) "Monitoring ${found.count { ticked[found.indexOf(it)] }} apps (${chosen.size} added)." else "Server replied ${r.code}."
                    } catch (e: Exception) {
                        "Saved on the phone; could not tell the server yet."
                    }
                }
            }
            .setNegativeButton("Cancel", null)
        if (!showAll) {
            builder.setNeutralButton("Show all apps") { _, _ ->
                pickBankApps(true, found.filterIndexed { i, _ -> ticked[i] }.map { it.packageName }.toSet())
            }
        }
        builder.show()
    }

    private fun refresh() {
        if (this::serverStatus.isInitialized && !serverStatus.text.startsWith("Testing")) {
            serverStatus.text = if (cfg.configured()) "Server: ${cfg.serverUrl} (key saved)" else "Server: NOT saved. Check the address and key below, then press «Save server & test connection»."
        }
        if (this::bgStatus.isInitialized) {
            val checks = backgroundChecks()
            bgStatus.text = BackgroundHelp.checklist(checks) + if (BackgroundHelp.allGood(checks)) "\nAll set." else ""
        }
        val listener = NotificationManagerCompat.getEnabledListenerPackages(this).contains(packageName)
        status.text = buildString {
            append(if (cfg.configured()) "Server & key: filled in\n" else "Server & key: NOT filled in\n")
            append(if (listener) "Notification access: active\n" else "Notification access: NOT active\n")
            append("Unsent queue: ${Outbox(this@SettingsActivity).pendingCount()}")
        }
        status.visibility = View.VISIBLE
    }

    override fun onResume() {
        super.onResume()
        refresh()
        if (cfg.configured()) {
            lifecycleScope.launch {
                runCatching {
                    val version = runCatching { packageManager.getPackageInfo(packageName, 0).versionName }.getOrNull() ?: "0.2.0"
                    Api(cfg).heartbeat(cfg.listenerUp, Outbox(this@SettingsActivity).pendingCount(), version)
                }
            }
        }
        lifecycleScope.launch { runCatching { LocationPush.push(this@SettingsActivity) } }  // keeps the weather's position fresh
    }
}
