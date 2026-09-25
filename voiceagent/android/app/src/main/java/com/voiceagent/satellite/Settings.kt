package com.voiceagent.satellite

import android.content.Context

class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("satellite", Context.MODE_PRIVATE)

    var server: String
        get() = prefs.getString("server", "") ?: ""
        set(value) = prefs.edit().putString("server", value).apply()

    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(value) = prefs.edit().putString("token", value).apply()

    var id: String
        get() = prefs.getString("id", "phone") ?: "phone"
        set(value) = prefs.edit().putString("id", value).apply()

    var chime: Boolean
        get() = prefs.getBoolean("chime", true)
        set(value) = prefs.edit().putBoolean("chime", value).apply()

    /** Chosen voice name for a language, or "" for automatic. */
    fun voice(lang: String): String = prefs.getString("voice_$lang", "") ?: ""

    fun setVoice(lang: String, name: String) = prefs.edit().putString("voice_$lang", name).apply()

    var speechRate: Float
        get() = prefs.getFloat("speech_rate", 1.0f)
        set(value) = prefs.edit().putFloat("speech_rate", value).apply()

    fun voicePrefs() = VoicePrefs(
        voices = Voices.LANGUAGES.associateWith { voice(it) }.filterValues { it.isNotEmpty() },
        rate = speechRate)

    fun toConfig() = SatelliteConfig(server = server, id = id, token = token, chime = chime)

    companion object {
        fun isValidServer(url: String) = url.startsWith("ws://") || url.startsWith("wss://")
    }
}
