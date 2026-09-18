package ch.zwaetschge.vocarium

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat

/**
 * Vordergrunddienst waehrend der Wiedergabe. Ohne ihn friert Android den
 * WebView-Prozess im Hintergrund nach kurzer Zeit ein und Hoerbuch oder
 * Hoerspiel verstummen. Die Benachrichtigung bietet Play/Pause und leitet
 * die Aktion ueber MainActivity an die Web-App zurueck.
 */
class PlaybackService : Service() {

    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
            else -> {
                val playing = intent?.getBooleanExtra(EXTRA_PLAYING, true) ?: true
                val title = intent?.getStringExtra(EXTRA_TITLE).orEmpty().ifBlank { "Vocarium" }
                val subtitle = intent?.getStringExtra(EXTRA_SUBTITLE).orEmpty()
                ensureChannel()
                startForeground(NOTIFICATION_ID, buildNotification(playing, title, subtitle))
                if (playing) acquireWakeLock() else releaseWakeLock()
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        releaseWakeLock()
        super.onDestroy()
    }

    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "vocarium:playback").apply {
            setReferenceCounted(false)
            acquire(6 * 60 * 60 * 1000L)
        }
    }

    private fun releaseWakeLock() {
        if (wakeLock?.isHeld == true) wakeLock?.release()
        wakeLock = null
    }

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Wiedergabe", NotificationManager.IMPORTANCE_LOW).apply {
                setShowBadge(false)
                setSound(null, null)
            },
        )
    }

    private fun buildNotification(playing: Boolean, title: String, subtitle: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        fun action(name: String, requestCode: Int) = PendingIntent.getActivity(
            this, requestCode,
            Intent(this, MainActivity::class.java).setAction(MainActivity.ACTION_MEDIA).putExtra(MainActivity.EXTRA_MEDIA_ACTION, name),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title)
            .setContentText(subtitle)
            .setContentIntent(open)
            .setOngoing(playing)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .addAction(R.drawable.ic_prev, "Zurück", action("prev", 1))
            .addAction(
                if (playing) R.drawable.ic_pause else R.drawable.ic_play,
                if (playing) "Pause" else "Weiter",
                action(if (playing) "pause" else "play", 2),
            )
            .addAction(R.drawable.ic_next, "Weiter", action("next", 3))
            .setStyle(androidx.media.app.NotificationCompat.MediaStyle().setShowActionsInCompactView(0, 1, 2))
            .build()
    }

    companion object {
        const val CHANNEL_ID = "vocarium_playback"
        const val NOTIFICATION_ID = 4711
        const val ACTION_STOP = "ch.zwaetschge.vocarium.STOP"
        const val EXTRA_PLAYING = "playing"
        const val EXTRA_TITLE = "title"
        const val EXTRA_SUBTITLE = "subtitle"
    }
}
