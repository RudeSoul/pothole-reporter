package com.gauravsen.potholereporter.drivemode

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import com.gauravsen.potholereporter.MainActivity
import com.gauravsen.potholereporter.R

class NotificationController(private val context: Context) {

    private val notificationManager =
        context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    init {
        createNotificationChannel()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                DriveConstants.NOTIFICATION_CHANNEL_ID,
                "Drive Mode Service",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Shows the active status of the pothole detection drive mode"
            }
            notificationManager.createNotificationChannel(channel)
        }
    }

    fun buildForegroundNotification(): Notification {
        return createNotificationBuilder("Initializing...", false, 0, 0).build()
    }

    fun updateNotification(status: String, isPaused: Boolean, found: Int, checked: Int) {
        val notification = createNotificationBuilder(status, isPaused, found, checked).build()
        notificationManager.notify(DriveConstants.NOTIFICATION_ID, notification)
    }

    private fun createNotificationBuilder(
        status: String,
        isPaused: Boolean,
        found: Int,
        checked: Int
    ): NotificationCompat.Builder {
        // Intent to open the main app
        val openAppIntent = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pendingOpenAppIntent = PendingIntent.getActivity(
            context,
            0,
            openAppIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        // Intent to stop the service
        val stopIntent = Intent(context, DriveModeService::class.java).apply {
            action = ACTION_STOP
        }
        val pendingStopIntent = PendingIntent.getService(
            context,
            1,
            stopIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        // Intent to pause or resume the service
        val toggleAction = if (isPaused) ACTION_RESUME else ACTION_PAUSE
        val toggleIntent = Intent(context, DriveModeService::class.java).apply {
            action = toggleAction
        }
        val pendingToggleIntent = PendingIntent.getService(
            context,
            2,
            toggleIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val toggleActionTitle = if (isPaused) {
            context.getString(R.string.drive_action_resume)
        } else {
            context.getString(R.string.drive_action_pause)
        }

        val contentText = "$status | Found: $found | Checked: $checked"

        return NotificationCompat.Builder(context, DriveConstants.NOTIFICATION_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_drive_notification)
            .setContentTitle(context.getString(R.string.drive_notification_title))
            .setContentText(contentText)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(pendingOpenAppIntent)
            .addAction(
                if (isPaused) android.R.drawable.ic_media_play else android.R.drawable.ic_media_pause,
                toggleActionTitle,
                pendingToggleIntent
            )
            .addAction(
                android.R.drawable.ic_menu_close_clear_cancel,
                context.getString(R.string.drive_action_stop),
                pendingStopIntent
            )
    }

    companion object {
        const val ACTION_PAUSE = "com.gauravsen.potholereporter.DRIVE_PAUSE"
        const val ACTION_RESUME = "com.gauravsen.potholereporter.DRIVE_RESUME"
        const val ACTION_STOP = "com.gauravsen.potholereporter.DRIVE_STOP"
    }
}
