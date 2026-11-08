package io.github.mdzaman.offlinevideo

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import kotlin.concurrent.thread

/**
 * Keeps the download engine running while the app is in the background, so downloads continue when
 * the screen is off or another app is open. Holds a wake lock only while something is downloading.
 */
class ServerService : Service() {
    private var wakeLock: PowerManager.WakeLock? = null
    @Volatile private var running = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            shutdown()
            return START_NOT_STICKY
        }
        val notification = notification()
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        if (!running) {
            running = true
            thread(name = "backend") {
                Backend.start(applicationContext)
                watchDownloads()
            }
        }
        return START_STICKY
    }

    // Android 15 limits background "data sync" work to 6 hours a day.
    override fun onTimeout(startId: Int, fgsType: Int) = shutdown()

    override fun onDestroy() {
        running = false
        wakeLock?.takeIf { it.isHeld }?.release()
        super.onDestroy()
    }

    private fun shutdown() {
        running = false
        thread { Backend.stop() }
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun watchDownloads() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        val lock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "OfflineVideo:downloads").apply { setReferenceCounted(false) }
        wakeLock = lock
        while (running) {
            if (Backend.hasActiveDownloads()) lock.acquire(10 * 60 * 1000L) else if (lock.isHeld) lock.release()
            Thread.sleep(15_000)
        }
        if (lock.isHeld) lock.release()
    }

    private fun notification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, getString(R.string.channel_name), NotificationManager.IMPORTANCE_LOW)
        )
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, ServerService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.notification_text))
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, getString(R.string.stop), stop).build())
            .build()
    }

    companion object {
        const val ACTION_STOP = "io.github.mdzaman.offlinevideo.STOP"
        private const val CHANNEL_ID = "engine"
        private const val NOTIFICATION_ID = 1
    }
}
