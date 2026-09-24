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

    fun toConfig() = SatelliteConfig(server = server, id = id, token = token, chime = chime)

    companion object {
        fun isValidServer(url: String) = url.startsWith("ws://") || url.startsWith("wss://")
    }
}
