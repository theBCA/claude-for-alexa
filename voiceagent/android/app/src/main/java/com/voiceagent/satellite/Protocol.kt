package com.voiceagent.satellite

import org.json.JSONException
import org.json.JSONObject

// This file and Voice.kt, SatelliteClient.kt and StatusBus.kt use no Android APIs,
// so the unit tests run them on a plain JVM against a real websocket server.

const val SAMPLE_RATE = 16000
const val CHUNK_BYTES = 2560 // 80 ms of 16 kHz mono int16, same as satellite.py

/** What the brain sends. The protocol is documented at the top of voiceagent/server.py. */
sealed interface BrainMessage {
    data object Wake : BrainMessage
    data class Say(val text: String, val lang: String?) : BrainMessage
    data object TurnEnd : BrainMessage
    data class Announce(val text: String, val lang: String?) : BrainMessage
    data object ConversationEnd : BrainMessage
    data object Sleep : BrainMessage
    data object Awake : BrainMessage
    data class Unknown(val raw: String) : BrainMessage
}

object Protocol {
    /** Close code the brain uses when the token is wrong. */
    const val CLOSE_UNAUTHORIZED = 4001

    fun hello(id: String, token: String): String =
        JSONObject().put("type", "hello").put("id", id).put("token", token).toString()

    fun speechDone(): String = JSONObject().put("type", "speech_done").toString()

    fun parse(raw: String): BrainMessage {
        val o = try {
            JSONObject(raw)
        } catch (e: JSONException) {
            return BrainMessage.Unknown(raw)
        }
        // optString turns JSON null into the string "null", and announce can carry "lang": null
        fun str(key: String): String? = if (o.isNull(key)) null else o.optString(key).ifBlank { null }
        return when (str("type")) {
            "wake" -> BrainMessage.Wake
            "say" -> BrainMessage.Say(str("text") ?: "", str("lang"))
            "turn_end" -> BrainMessage.TurnEnd
            "announce" -> BrainMessage.Announce(str("text") ?: "", str("lang"))
            "conversation_end" -> BrainMessage.ConversationEnd
            "sleep" -> BrainMessage.Sleep
            "awake" -> BrainMessage.Awake
            else -> BrainMessage.Unknown(raw)
        }
    }
}

fun interface Logger {
    fun log(message: String, error: Throwable?)

    companion object {
        val NONE = Logger { _, _ -> }
    }
}
