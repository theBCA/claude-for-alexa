package com.voiceagent.satellite

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.drawable.Icon
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log

/**
 * Keeps the satellite running with the screen off. Android only allows an
 * always-on mic from a foreground service with a visible notification.
 */
class SatelliteService : Service() {
    @Volatile
    private var client: SatelliteClient? = null
    private var tts: AndroidTts? = null
    private var chime: ToneChime? = null
    private var voice: Voice? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    private val log = Logger { message, error -> Log.i(TAG, message, error) }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            shutdown()
            stopSelf()
            return START_NOT_STICKY
        }
        try {
            // every startForegroundService call must be answered with startForeground
            goForeground(Status.Connecting.describe())
        } catch (e: Exception) {
            // Android 14+ refuses a mic service started from the background,
            // for example when the system restarts us after killing the app.
            log.log("could not start in the foreground", e)
            StatusBus.post("Stopped. Open the app and press Start.")
            shutdown()
            stopSelf()
            return START_NOT_STICKY
        }

        val settings = Settings(this)
        if (!Settings.isValidServer(settings.server)) {
            shutdown()
            StatusBus.post("Set the brain address first")
            stopSelf()
            return START_NOT_STICKY
        }

        // Start while running means the settings changed: reconnect with the new ones.
        stopClient()
        if (wakeLock == null) acquireLocks()
        val tts = AndroidTts(this, log).also { this.tts = it }
        val chime = ToneChime().also { this.chime = it }
        val config = settings.toConfig()
        val voice = Voice(tts, config.postSpeechMuteMs, log).also { this.voice = it }
        lateinit var created: SatelliteClient
        created = SatelliteClient(
            config = config,
            micFactory = { AndroidMic() },
            voice = voice,
            chime = chime,
            // a client being replaced reports Stopped, which must not stop the service
            onStatus = { if (client === created) onStatus(it) },
            log = log,
        )
        client = created
        created.start()
        return START_STICKY
    }

    override fun onDestroy() {
        shutdown()
        super.onDestroy()
    }

    private fun onStatus(status: Status) {
        val text = status.describe()
        StatusBus.post(text)
        if (status == Status.Rejected || status == Status.Stopped) {
            stopSelf()
            return
        }
        getSystemService(NotificationManager::class.java)!!.notify(NOTIFICATION_ID, notification(text))
    }

    private fun stopClient() {
        val old = client
        client = null
        old?.stop()
        voice?.shutdown()
        voice = null
        tts?.shutdown()
        tts = null
        chime?.release()
        chime = null
    }

    private fun shutdown() {
        stopClient()
        wakeLock?.takeIf { it.isHeld }?.release()
        wakeLock = null
        wifiLock?.takeIf { it.isHeld }?.release()
        wifiLock = null
        if (StatusBus.last != Status.Rejected.describe()) StatusBus.post(Status.Stopped.describe())
    }

    @Suppress("DEPRECATION") // WIFI_MODE_FULL_HIGH_PERF still keeps Wi-Fi awake with the screen off
    private fun acquireLocks() {
        wakeLock = getSystemService(PowerManager::class.java)!!
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "voiceagent:satellite")
            .apply { acquire() }
        wifiLock = (applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager)
            .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "voiceagent:satellite")
            .apply { acquire() }
    }

    private fun goForeground(text: String) {
        val manager = getSystemService(NotificationManager::class.java)!!
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Satellite", NotificationManager.IMPORTANCE_LOW))
        val notification = notification(text)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val stop = PendingIntent.getService(
            this, 1, Intent(this, SatelliteService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_mic)
            .setContentTitle("Voiceagent")
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null as Icon?, "Stop", stop).build())
            .build()
    }

    companion object {
        private const val TAG = "voiceagent"
        private const val CHANNEL_ID = "satellite"
        private const val NOTIFICATION_ID = 1
        private const val ACTION_STOP = "stop"

        fun start(context: Context) {
            context.startForegroundService(Intent(context, SatelliteService::class.java))
        }

        fun stop(context: Context) {
            context.startService(Intent(context, SatelliteService::class.java).setAction(ACTION_STOP))
        }
    }
}
