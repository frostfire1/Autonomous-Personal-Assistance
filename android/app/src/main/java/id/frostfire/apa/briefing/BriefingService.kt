package id.frostfire.apa.briefing

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.media.MediaPlayer
import android.os.IBinder
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import id.frostfire.apa.core.Api
import id.frostfire.apa.core.Config
import id.frostfire.apa.ui.SettingsActivity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.File
import java.util.Locale

/**
 * Plays the server's briefing aloud. The voice is natural neural speech generated on the server (one MP3 per topic,
 * the next one fetched while the current one plays); if the server cannot make audio, the rest is read with
 * Android's own text-to-speech instead, so the briefing is never silent. Runs as a foreground service so the system
 * does not stop it. Respects audio focus (music is ducked, a phone call is not talked over) and offers Skip (next
 * topic) and Stop on the notification.
 */
class BriefingService : Service(), TextToSpeech.OnInitListener {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    private var pending: Plan? = null
    private var current = 0
    private var focus: AudioFocusRequest? = null
    private var player: MediaPlayer? = null
    private var natural = false
    private var ttsFrom = 0
    private val clips = HashMap<Int, Deferred<File?>>()

    /** What to say: one list of pieces per section, so Skip can jump to the next topic. */
    private class Plan(val id: Long, val topics: List<Pair<String, List<String>>>)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> { finish(played = false); return START_NOT_STICKY }
            ACTION_SKIP -> { skip(); return START_NOT_STICKY }
        }
        createChannel()
        startForeground(NOTIF_ID, notification("Preparing your briefing…", withActions = false))
        val kind = intent?.getStringExtra(EXTRA_KIND) ?: "short"
        val manual = intent?.getStringExtra("reason") == "manual"
        scope.launch {
            val cfg = Config(this@BriefingService)
            runCatching { id.frostfire.apa.location.LocationPush.push(this@BriefingService) }  // best effort, only if GPS weather is on
            if (manual) runCatching { Api(cfg).sendBriefingToTelegram() }  // best effort; the voice plays either way
            try {
                val r = Api(cfg).briefing(kind)
                if (!r.ok) return@launch fail("Server rejected the request (${r.code}). Check the API key.")
                val j = JSONObject(r.body)
                val sections = j.getJSONArray("sections")
                val topics = (0 until sections.length()).map {
                    val s = sections.getJSONObject(it)
                    s.getString("topic") to Speech.chunk(s.getString("text"))
                }
                pending = Plan(j.getLong("id"), topics)
                if (!requestFocus()) return@launch finish(played = false)  // a call or navigation is speaking: stay quiet
                natural = true
                playNatural(0)
            } catch (e: Exception) {
                fail("Could not reach the server.")  // silent: nobody wants an error spoken at 6 in the morning
            }
        }
        return START_NOT_STICKY
    }

    private fun startSpeaking(from: Int) {
        natural = false
        ttsFrom = from
        tts = TextToSpeech(this, this)
    }

    /** The clip for one topic, downloaded once (the next topic is requested early so playback has no gaps). */
    private fun clip(plan: Plan, index: Int): Deferred<File?>? {
        if (index >= plan.topics.size) return null
        return clips.getOrPut(index) {
            scope.async(Dispatchers.IO) {
                Api(Config(this@BriefingService)).audio(plan.id, index)?.let {
                    File(cacheDir, "brief_${plan.id}_$index.mp3").also { f -> f.writeBytes(it) }
                }
            }
        }
    }

    private fun playNatural(index: Int) {
        val plan = pending ?: return
        current = index
        if (index >= plan.topics.size) return finish(played = true)
        updateNotification(plan.topics[index].first)
        scope.launch {
            val file = clip(plan, index)?.await() ?: return@launch startSpeaking(index)  // no audio: speak the rest ourselves
            clip(plan, index + 1)
            player?.release()
            val mp = MediaPlayer()
            player = mp
            try {
                mp.setAudioAttributes(
                    AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build(),
                )
                mp.setDataSource(file.absolutePath)
                mp.setOnCompletionListener { if (player === mp) playNatural(index + 1) }
                mp.setOnErrorListener { _, _, _ -> if (player === mp) startSpeaking(index); true }
                mp.prepare()
                mp.start()
            } catch (e: Exception) {
                startSpeaking(index)
            }
        }
    }

    override fun onInit(status: Int) {
        val t = tts ?: return
        if (status != TextToSpeech.SUCCESS) return fail("Speech engine unavailable.")
        val lang = t.setLanguage(Locale("id", "ID"))
        if (lang == TextToSpeech.LANG_MISSING_DATA || lang == TextToSpeech.LANG_NOT_SUPPORTED) t.setLanguage(Locale.getDefault())
        t.setAudioAttributes(
            AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANCE_ACCESSIBILITY)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build(),
        )
        t.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) = Unit
            override fun onError(utteranceId: String?) = fail("Could not read the briefing aloud.")
            override fun onDone(utteranceId: String?) {
                if (utteranceId == LAST) scope.launch { finish(played = true) }
            }
        })
        ttsReady = true
        player?.release(); player = null
        speakFrom(ttsFrom)
    }

    private fun speakFrom(index: Int) {
        val plan = pending ?: return
        val t = tts ?: return
        if (!ttsReady) return
        current = index
        t.stop()
        val rest = plan.topics.drop(index)
        if (rest.isEmpty()) return finish(played = true)
        updateNotification(rest.first().first)
        var n = 0
        val total = rest.sumOf { it.second.size }
        for ((_, pieces) in rest) for (p in pieces) {
            n++
            t.speak(p, TextToSpeech.QUEUE_ADD, null, if (n == total) LAST else "u$n")
        }
    }

    private fun skip() = if (natural) playNatural(current + 1) else speakFrom(current + 1)

    private fun finish(played: Boolean) {
        val plan = pending
        pending = null
        scope.launch {
            if (played && plan != null) {
                val cfg = Config(this@BriefingService)
                cfg.markPlayedNow()
                runCatching { Api(cfg).markPlayed(plan.id) }  // best effort: the briefing was heard either way
            }
            tts?.stop(); tts?.shutdown(); tts = null
            player?.release(); player = null
            clips.clear()
            cacheDir.listFiles { f -> f.name.startsWith("brief_") }?.forEach { it.delete() }
            focus?.let { (getSystemService(Context.AUDIO_SERVICE) as AudioManager).abandonAudioFocusRequest(it) }
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
    }

    private fun fail(message: String) {
        updateNotification(message, withActions = false)
        scope.launch { kotlinx.coroutines.delay(4_000); finish(played = false) }
    }

    private fun requestFocus(): Boolean {
        val am = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val req = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
            .setAudioAttributes(
                AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANCE_ACCESSIBILITY)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build(),
            ).build()
        focus = req
        return am.requestAudioFocus(req) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
    }

    private fun createChannel() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Morning briefing", NotificationManager.IMPORTANCE_LOW))
    }

    private fun action(label: String, action: String): Notification.Action {
        val pi = PendingIntent.getService(
            this, action.hashCode(), Intent(this, BriefingService::class.java).setAction(action),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return Notification.Action.Builder(null, label, pi).build()
    }

    private fun notification(text: String, withActions: Boolean = true): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, SettingsActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val b = Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_lock_silent_mode_off).setContentTitle("Morning briefing")
            .setContentText(text).setContentIntent(open).setOngoing(true)
        if (withActions) b.addAction(action("Skip", ACTION_SKIP)).addAction(action("Stop", ACTION_STOP))
        return b.build()
    }

    private fun updateNotification(text: String, withActions: Boolean = true) {
        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).notify(NOTIF_ID, notification(text, withActions))
    }

    override fun onDestroy() {
        tts?.shutdown()
        player?.release()
        scope.cancel()
        super.onDestroy()
    }

    companion object {
        private const val CHANNEL = "briefing"
        private const val NOTIF_ID = 4101
        private const val EXTRA_KIND = "kind"
        private const val LAST = "last"
        const val ACTION_STOP = "id.frostfire.apa.STOP"
        const val ACTION_SKIP = "id.frostfire.apa.SKIP"

        /**
         * Starts the briefing. Android may refuse to start a foreground service from the background; then a
         * notification with a "Play" button is posted instead (a tap is always allowed).
         */
        fun start(ctx: Context, kind: String, reason: String) {
            val intent = Intent(ctx, BriefingService::class.java).putExtra(EXTRA_KIND, kind).putExtra("reason", reason)
            try {
                ctx.startForegroundService(intent)
            } catch (e: Exception) {
                val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
                nm.createNotificationChannel(NotificationChannel(CHANNEL, "Morning briefing", NotificationManager.IMPORTANCE_DEFAULT))
                val pi = PendingIntent.getForegroundService(ctx, 1, intent, PendingIntent.FLAG_IMMUTABLE)
                nm.notify(
                    NOTIF_ID + 1,
                    Notification.Builder(ctx, CHANNEL).setSmallIcon(android.R.drawable.ic_lock_silent_mode_off)
                        .setContentTitle("Briefing ready").setContentText("Tap to play").setContentIntent(pi)
                        .setAutoCancel(true).build(),
                )
            }
        }
    }
}
