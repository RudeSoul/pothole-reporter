package com.gauravsen.potholereporter

import com.getcapacitor.BridgeActivity

/**
 * Main activity for the Pothole Reporter Capacitor app.
 *
 * Registers the native DriveModePlugin so the WebView can start and
 * control the background Drive Mode service via the Capacitor bridge.
 */
class MainActivity : BridgeActivity() {
    override fun onCreate(savedInstanceState: android.os.Bundle?) {
        registerPlugin(com.gauravsen.potholereporter.bridge.DriveModePlugin::class.java)
        super.onCreate(savedInstanceState)
        // Recover any accepted observation committed just before a process death.
        com.gauravsen.potholereporter.drivemode.UploadWorker.enqueue(
            applicationContext,
            ensureAfterCurrent = true,
        )
    }
}
