package com.voiceagent.satellite

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings as SystemSettings
import android.speech.tts.TextToSpeech
import android.text.InputType
import android.view.View
import android.view.WindowInsets
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView

/** Settings screen: brain address, token, room id, voices, and start/stop. */
class MainActivity : Activity() {
    private lateinit var settings: Settings
    private lateinit var server: EditText
    private lateinit var token: EditText
    private lateinit var roomId: EditText
    private lateinit var chime: CheckBox
    private lateinit var status: TextView
    private lateinit var battery: Button
    private lateinit var voiceRows: LinearLayout
    private var unlisten: (() -> Unit)? = null

    /** Separate engine instance for listing and previewing voices; the service has its own. */
    private var preview: TextToSpeech? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = Settings(this)
        setContentView(buildUi())
        preview = Voices.engine(this) { status ->
            runOnUiThread { if (status == TextToSpeech.SUCCESS) fillVoiceRows() else showNoTts() }
        }
    }

    override fun onDestroy() {
        preview?.shutdown()
        preview = null
        super.onDestroy()
    }

    override fun onResume() {
        super.onResume()
        status.text = StatusBus.last
        unlisten = StatusBus.listen { text -> runOnUiThread { status.text = text } }
        battery.visibility = if (ignoresBatteryOptimizations()) View.GONE else View.VISIBLE
    }

    override fun onPause() {
        unlisten?.invoke()
        unlisten = null
        save()
        super.onPause()
    }

    private fun buildUi(): View {
        val pad = dp(16)
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }

        fun label(text: String) = column.addView(TextView(this).apply {
            this.text = text
            setPadding(0, dp(12), 0, dp(4))
        })

        label("Brain address")
        server = EditText(this).apply {
            hint = "ws://192.168.1.20:8765"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setText(settings.server)
        }
        column.addView(server)

        label("Token (server.token in the brain's config.yaml)")
        token = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setText(settings.token)
        }
        column.addView(token)

        label("Room id")
        roomId = EditText(this).apply {
            hint = "living-room"
            inputType = InputType.TYPE_CLASS_TEXT
            setText(settings.id)
        }
        column.addView(roomId)

        chime = CheckBox(this).apply {
            text = "Play a chime on wake word"
            isChecked = settings.chime
        }
        column.addView(chime)

        label("Voices (changes apply after pressing Start again)")
        voiceRows = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        voiceRows.addView(TextView(this).apply { text = "Loading voices..." })
        column.addView(voiceRows)

        val buttons = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(16), 0, 0)
        }
        buttons.addView(Button(this).apply {
            text = "Start"
            setOnClickListener { onStartPressed() }
        })
        buttons.addView(Button(this).apply {
            text = "Stop"
            setOnClickListener { SatelliteService.stop(this@MainActivity) }
        })
        column.addView(buttons)

        status = TextView(this).apply {
            textSize = 18f
            setPadding(0, dp(16), 0, dp(16))
        }
        column.addView(status)

        battery = Button(this).apply {
            text = "Let it run in the background"
            setOnClickListener { askToIgnoreBatteryOptimizations() }
        }
        column.addView(battery)

        column.addView(TextView(this).apply {
            text = "Android may stop the app when the phone sleeps unless battery optimization is off for it. " +
                "Pair the phone with the Echo Dot over Bluetooth (\"Alexa, pair\") to hear replies through it."
            setPadding(0, dp(8), 0, 0)
        })

        return ScrollView(this).apply {
            addView(column)
            // targetSdk 35 draws behind the status and navigation bars
            setOnApplyWindowInsetsListener { v, insets ->
                val (left, top, right, bottom) = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                    val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.ime())
                    listOf(bars.left, bars.top, bars.right, bars.bottom)
                } else {
                    @Suppress("DEPRECATION")
                    listOf(insets.systemWindowInsetLeft, insets.systemWindowInsetTop,
                        insets.systemWindowInsetRight, insets.systemWindowInsetBottom)
                }
                v.setPadding(left, top, right, bottom)
                insets
            }
        }
    }

    private fun fillVoiceRows() {
        val tts = preview ?: return
        voiceRows.removeAllViews()
        val all = try {
            tts.voices
        } catch (e: RuntimeException) {
            null
        }
        for (lang in Voices.LANGUAGES) {
            val voices = Voices.installed(all, lang)
            val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            row.addView(TextView(this).apply {
                text = lang.uppercase()
                width = dp(36)
            })
            if (voices.isEmpty()) {
                row.addView(TextView(this).apply { text = "No voice installed. Use \"Download voices\" below." })
                voiceRows.addView(row)
                continue
            }
            val names = listOf("") + voices.map { it.name }
            val labels = listOf("Automatic (${Voices.label(voices.first())})") + voices.map { Voices.label(it) }
            val spinner = Spinner(this).apply {
                adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_dropdown_item, labels)
                setSelection(names.indexOf(settings.voice(lang)).coerceAtLeast(0))
                onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
                    override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                        settings.setVoice(lang, names[position])
                    }

                    override fun onNothingSelected(parent: AdapterView<*>?) {}
                }
            }
            row.addView(spinner, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            row.addView(Button(this).apply {
                text = "Test"
                setOnClickListener { previewVoice(lang, names[spinner.selectedItemPosition]) }
            })
            voiceRows.addView(row)
        }

        val rates = listOf(0.9f, 1.0f, 1.1f, 1.2f)
        val rateRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        rateRow.addView(TextView(this).apply { text = "Speed" })
        rateRow.addView(Spinner(this).apply {
            adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_dropdown_item,
                listOf("Slower", "Normal", "A bit faster", "Faster"))
            setSelection(rates.indexOf(settings.speechRate).takeIf { it >= 0 } ?: 1)
            onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
                override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                    settings.speechRate = rates[position]
                }

                override fun onNothingSelected(parent: AdapterView<*>?) {}
            }
        })
        voiceRows.addView(rateRow)

        val engine = tts.defaultEngine
        voiceRows.addView(TextView(this).apply {
            text = if (tts.engines.any { it.name == Voices.GOOGLE_TTS }) {
                "Using Speech Services by Google. \"(online)\" voices sound most natural but need internet."
            } else {
                "Speech Services by Google isn't installed (current engine: $engine). " +
                    "Install it from the Play Store for much more natural voices."
            }
            setPadding(0, dp(8), 0, 0)
        })
        voiceRows.addView(Button(this).apply {
            text = "Download voices"
            setOnClickListener { openVoiceDownloads() }
        })
    }

    private fun showNoTts() {
        voiceRows.removeAllViews()
        voiceRows.addView(TextView(this).apply {
            text = "Text to speech isn't available. Install Speech Services by Google from the Play Store."
        })
    }

    private fun previewVoice(lang: String, name: String) {
        val tts = preview ?: return
        tts.setSpeechRate(settings.speechRate)
        tts.language = Voices.localeFor(lang)
        Voices.pick(tts.voices, lang, name)?.let { tts.voice = it }
        tts.speak(SAMPLES[lang] ?: SAMPLES.getValue("en"), TextToSpeech.QUEUE_FLUSH, null, "preview")
    }

    private fun openVoiceDownloads() {
        val install = Intent(TextToSpeech.Engine.ACTION_INSTALL_TTS_DATA).setPackage(Voices.GOOGLE_TTS)
        try {
            startActivity(install)
        } catch (e: android.content.ActivityNotFoundException) {
            startActivity(Intent("com.android.settings.TTS_SETTINGS"))
        }
    }

    private fun save() {
        settings.server = server.text.toString().trim()
        settings.token = token.text.toString().trim()
        settings.id = roomId.text.toString().trim().ifEmpty { "phone" }
        settings.chime = chime.isChecked
    }

    private fun onStartPressed() {
        save()
        if (!Settings.isValidServer(settings.server)) {
            status.text = "The brain address must start with ws:// or wss://"
            return
        }
        val missing = requiredPermissions().filter {
            checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) startService() else requestPermissions(missing.toTypedArray(), REQUEST_PERMISSIONS)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_PERMISSIONS) return
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            startService() // the notification permission is optional, the service runs without it
        } else {
            status.text = "The microphone permission is needed. Allow it in Android settings, Apps, Voiceagent."
        }
    }

    private fun startService() {
        SatelliteService.start(this) // if already running, it reconnects with the new settings
    }

    private fun requiredPermissions(): List<String> =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            listOf(Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS)
        } else {
            listOf(Manifest.permission.RECORD_AUDIO)
        }

    private fun ignoresBatteryOptimizations(): Boolean =
        getSystemService(PowerManager::class.java)!!.isIgnoringBatteryOptimizations(packageName)

    @SuppressLint("BatteryLife") // a sideloaded always-on assistant is the case this exists for
    private fun askToIgnoreBatteryOptimizations() {
        startActivity(Intent(SystemSettings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            .setData(Uri.parse("package:$packageName")))
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    companion object {
        private const val REQUEST_PERMISSIONS = 1
        private val SAMPLES = mapOf(
            "en" to "Hi. The living room lights are off, and the vacuum is on its way to the kitchen.",
            "tr" to "Merhaba. Salondaki ışıkları kapattım, süpürge de mutfağa gidiyor.",
            "de" to "Hallo. Das Licht im Wohnzimmer ist aus, und der Staubsauger fährt in die Küche.",
        )
    }
}
