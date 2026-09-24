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

/** Android's built-in TextToSpeech, switching voice by the language Whisper detected. */
class AndroidTts(context: Context, private val log: Logger) : Tts {
    private val ready = CountDownLatch(1)

    @Volatile
    private var ok = false
    private val pending = ConcurrentHashMap<String, CountDownLatch>()
    private var currentLang: String? = null

    private val tts: TextToSpeech = TextToSpeech(context.applicationContext) { status ->
        ok = status == TextToSpeech.SUCCESS
        if (!ok) log.log("text to speech failed to start ($status)", null)
        ready.countDown()
    }

    init {
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
        val result = tts.setLanguage(Locale.forLanguageTag(lang))
        if (result == TextToSpeech.LANG_MISSING_DATA || result == TextToSpeech.LANG_NOT_SUPPORTED) {
            log.log("no voice installed for '$lang', using the default", null)
            tts.setLanguage(Locale.getDefault())
        }
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
