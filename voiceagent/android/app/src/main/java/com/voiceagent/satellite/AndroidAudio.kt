package com.voiceagent.satellite

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.ToneGenerator
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.speech.tts.Voice as TtsVoice
import java.util.Locale
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** 16 kHz mono int16 from the phone's mic, tuned for speech recognition. */
class AndroidMic : Mic {
    private var record: AudioRecord? = null

    @SuppressLint("MissingPermission") // the activity asks for RECORD_AUDIO before starting the service
    override fun start() {
        val min = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val r = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION, SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
            maxOf(min, CHUNK_BYTES * 8))
        if (r.state != AudioRecord.STATE_INITIALIZED) {
            r.release()
            throw IllegalStateException("microphone unavailable")
        }
        r.startRecording()
        record = r
    }

    override fun read(buf: ByteArray): Boolean {
        val r = record ?: return false
        var off = 0
        while (off < buf.size) {
            val n = r.read(buf, off, buf.size - off)
            if (n <= 0) return false
            off += n
        }
        return true
    }

    override fun stop() {
        record?.let {
            try {
                it.stop()
            } catch (e: IllegalStateException) {
                // already stopped
            }
            it.release()
        }
        record = null
    }
}

/** Voice choice per language ("en", "tr", "de" -> voice name) and speaking speed, from Settings. */
data class VoicePrefs(val voices: Map<String, String> = emptyMap(), val rate: Float = 1.0f)

object Voices {
    /** Speech Services by Google: neural voices for many languages. Many phones default to a more robotic engine. */
    const val GOOGLE_TTS = "com.google.android.tts"
    val LANGUAGES = listOf("en", "tr", "de")
    private val home = mapOf("en" to "US", "tr" to "TR", "de" to "DE")

    /** Installed voices for a language, best first: home country, quality, on-device before online. */
    fun installed(all: Set<TtsVoice>?, lang: String): List<TtsVoice> = all.orEmpty()
        .filter { it.locale.language == lang && TextToSpeech.Engine.KEY_FEATURE_NOT_INSTALLED !in it.features }
        .sortedWith(compareByDescending<TtsVoice> { it.locale.country == home[lang] }
            .thenByDescending { it.quality }
            .thenBy { it.isNetworkConnectionRequired }
            .thenBy { it.name })

    fun pick(all: Set<TtsVoice>?, lang: String, preferred: String?): TtsVoice? {
        val list = installed(all, lang)
        return list.firstOrNull { it.name == preferred } ?: list.firstOrNull()
    }

    fun label(v: TtsVoice): String =
        v.name + (if (v.isNetworkConnectionRequired) " (online)" else "") + " · ${v.locale.displayCountry}"

    fun localeFor(lang: String): Locale = Locale.forLanguageTag(home[lang]?.let { "$lang-$it" } ?: lang)

    /** Creates a TextToSpeech on Google's engine; Android falls back to the default engine if it isn't installed. */
    fun engine(context: Context, onInit: TextToSpeech.OnInitListener) =
        TextToSpeech(context.applicationContext, onInit, GOOGLE_TTS)
}

/** Android's TextToSpeech, switching voice by the language Whisper detected. */
class AndroidTts(
    context: Context,
    private val log: Logger,
    private val prefs: VoicePrefs = VoicePrefs(),
) : Tts {
    private val ready = CountDownLatch(1)

    @Volatile
    private var ok = false
    private val pending = ConcurrentHashMap<String, CountDownLatch>()
    private var currentLang: String? = null

    private val tts: TextToSpeech = Voices.engine(context) { status ->
        ok = status == TextToSpeech.SUCCESS
        if (!ok) log.log("text to speech failed to start ($status)", null)
        ready.countDown()
    }

    init {
        tts.setSpeechRate(prefs.rate)
        tts.setAudioAttributes(
            AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build())
        tts.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String) {}

            override fun onDone(utteranceId: String) {
                pending.remove(utteranceId)?.countDown()
            }

            @Deprecated("Deprecated in Java")
            override fun onError(utteranceId: String) {
                pending.remove(utteranceId)?.countDown()
            }

            override fun onError(utteranceId: String, errorCode: Int) {
                pending.remove(utteranceId)?.countDown()
            }

            override fun onStop(utteranceId: String, interrupted: Boolean) {
                pending.remove(utteranceId)?.countDown()
            }
        })
    }

    override fun speak(text: String, lang: String?) {
        if (!ready.await(10, TimeUnit.SECONDS) || !ok || text.isBlank()) return
        useLanguage(lang ?: "en")
        val id = UUID.randomUUID().toString()
        val done = CountDownLatch(1)
        pending[id] = done
        if (tts.speak(text, TextToSpeech.QUEUE_ADD, null, id) != TextToSpeech.SUCCESS) {
            pending.remove(id)
            log.log("text to speech refused a sentence", null)
            return
        }
        // generous upper bound so a lost callback can't keep the mic muted forever
        done.await(15 + text.length / 5L, TimeUnit.SECONDS)
    }

    private fun useLanguage(lang: String) {
        if (lang == currentLang) return
        currentLang = lang
        val result = tts.setLanguage(Voices.localeFor(lang))
        if (result == TextToSpeech.LANG_MISSING_DATA || result == TextToSpeech.LANG_NOT_SUPPORTED) {
            log.log("no voice installed for '$lang', using the default", null)
            tts.setLanguage(Locale.getDefault())
            return
        }
        // some engines throw from getVoices; the language's default voice is fine then
        val voice = try {
            Voices.pick(tts.voices, lang, prefs.voices[lang])
        } catch (e: RuntimeException) {
            null
        }
        if (voice != null) tts.voice = voice
    }

    fun shutdown() {
        tts.stop()
        tts.shutdown()
    }
}

class ToneChime : Chime {
    private val tones = ToneGenerator(AudioManager.STREAM_MUSIC, 60)

    override fun start() {
        tones.startTone(ToneGenerator.TONE_PROP_BEEP, 150)
    }

    override fun end() {
        tones.startTone(ToneGenerator.TONE_PROP_BEEP2, 150)
    }

    fun release() {
        tones.release()
    }
}
