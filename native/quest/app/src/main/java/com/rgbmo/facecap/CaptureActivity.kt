package com.rgbmo.facecap

import android.app.Activity
import android.os.Bundle
import android.widget.TextView

class CaptureActivity : Activity() {
    private var base = ""
    @Volatile var running = true
    @Volatile var recording = false
    @Volatile var epoch = Double.NaN
    @Volatile var offset = 0.0
    private lateinit var note: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        base = intent.getStringExtra("url")?.trimEnd('/') ?: return finish()
        note = TextView(this)
        note.text = "Starting face and eye tracking"
        setContentView(note)
        try {
            System.loadLibrary("openxr_loader")
        } catch (_: UnsatisfiedLinkError) {
        }
        System.loadLibrary("rgbfacecap")
        Thread { poll() }.start()
        Thread { nativeRun(base) }.start()
    }

    override fun onDestroy() {
        running = false
        super.onDestroy()
    }

    private fun poll() {
        Net.post("$base/hello", "{\"role\":\"quest\"}")
        while (running) {
            val started = System.nanoTime()
            val body = Net.get("$base/status")
            val rtt = (System.nanoTime() - started) / 1_000_000_000.0
            if (body != null) {
                recording = body.contains("\"recording\": true") || body.contains("\"recording\":true")
                epoch = number(body, "epoch") ?: Double.NaN
                val server = number(body, "server_time")
                if (server != null) offset = server - System.currentTimeMillis() / 1000.0 - rtt / 2.0
            }
            try {
                Thread.sleep(200)
            } catch (_: InterruptedException) {
                return
            }
        }
    }

    private fun number(json: String, key: String): Double? {
        val token = "\"$key\":"
        val at = json.indexOf(token)
        if (at < 0) return null
        val raw = json.substring(at + token.length).trimStart()
        if (raw.startsWith("null")) return null
        val end = raw.indexOfFirst { it == ',' || it == '}' || it.isWhitespace() }
        return raw.substring(0, if (end < 0) raw.length else end).toDoubleOrNull()
    }

    fun clock(): Double {
        if (!recording || epoch.isNaN()) return -1.0
        return System.currentTimeMillis() / 1000.0 + offset - epoch
    }

    fun postFrame(body: String) {
        Net.post("$base/frame", body)
    }

    fun toggleRecord() {
        val path = if (recording) "/stop" else "/start"
        Net.post("$base$path", "{}")
    }

    fun note(text: String) {
        runOnUiThread { note.text = text }
    }

    private external fun nativeRun(url: String)
}
