package com.rgbmo.facecap

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

class SetupActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = getSharedPreferences("rgb", MODE_PRIVATE)
        val url = EditText(this)
        url.setText(prefs.getString("url", "https://192.168.1.160:8765"))
        val note = TextView(this)
        note.text = "Use the address from the RGB MoCap viewport. Accept the certificate on the PC side by connecting anyway. The A button starts and stops the take."
        val enter = Button(this)
        enter.text = "Enter MR"
        enter.setOnClickListener {
            val value = url.text.toString().trim().trimEnd('/')
            prefs.edit().putString("url", value).apply()
            startActivity(Intent(this, CaptureActivity::class.java).putExtra("url", value))
        }
        val record = Button(this)
        record.text = "Record / Stop"
        record.setOnClickListener {
            val base = url.text.toString().trim().trimEnd('/')
            Thread {
                val status = Net.get("$base/status").orEmpty()
                val path = if (status.contains("\"recording\": true") || status.contains("\"recording\":true")) "/stop" else "/start"
                val ok = Net.post("$base$path", "{}")
                runOnUiThread { note.text = if (ok) "Sent $path" else "Could not reach $base" }
            }.start()
        }
        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
            addView(url)
            addView(enter)
            addView(record)
            addView(note)
        })
    }
}
