package com.voiceagent.satellite

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString.Companion.toByteString
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

interface Mic {
    fun start()

    /** Fills [buf] completely. Returns false if the mic stopped or failed. */
    fun read(buf: ByteArray): Boolean

    fun stop()
}

interface Chime {
    fun start()
    fun end()
}

data class SatelliteConfig(
    val server: String,
    val id: String,
    val token: String,
    val chime: Boolean = true,
    val postSpeechMuteMs: Long = 600,
)

sealed interface Status {
    data object Connecting : Status
    data object Idle : Status
    data object Listening : Status
    data object Speaking : Status
    data object Sleeping : Status
    data class Retrying(val seconds: Long, val reason: String) : Status
    data object Rejected : Status
    data object Stopped : Status

    fun describe(): String = when (this) {
        Connecting -> "Connecting to the brain"
        Idle -> "Waiting for the wake word"
        Listening -> "Listening"
        Speaking -> "Speaking"
        Sleeping -> "Sleeping. Say \"Hey Jarvis, wake up\""
        is Retrying -> "Connection lost ($reason), retrying in ${seconds}s"
        Rejected -> "The brain rejected the token. Check it in the app."
        Stopped -> "Stopped"
    }
}

/**
 * The satellite side of the protocol in voiceagent/server.py: connect, say hello,
 * stream mic audio while not speaking, speak what comes back, reconnect with
 * backoff. Same behaviour as satellite.py.
 */
class SatelliteClient(
    private val config: SatelliteConfig,
    private val micFactory: () -> Mic,
    private val voice: Voice,
    private val chime: Chime,
    private val onStatus: (Status) -> Unit = {},
    private val log: Logger = Logger.NONE,
    private val http: OkHttpClient = defaultHttpClient(),
) {
    private sealed interface Event {
        data object Opened : Event
        class Ended(val code: Int, val reason: String) : Event
    }

    @Volatile
    private var running = false

    @Volatile
    private var socket: WebSocket? = null

    @Volatile
    private var inConversation = false
    private var asleep = false

    /** What to show between conversations. */
    private fun resting(): Status = if (asleep) Status.Sleeping else Status.Idle

    private var loopThread: Thread? = null

    fun start() {
        if (running) return
        running = true
        loopThread = thread(name = "satellite") { loop() }
    }

    fun stop() {
        running = false
        socket?.close(1000, "bye")
        loopThread?.interrupt()
        loopThread?.join(3000)
        loopThread = null
    }

    private fun loop() {
        var backoff = 1L
        while (running) {
            onStatus(Status.Connecting)
            val ended = session()
            if (!running) break
            if (ended.code == Protocol.CLOSE_UNAUTHORIZED) {
                running = false
                onStatus(Status.Rejected)
                return
            }
            if (ended.opened) backoff = 1
            onStatus(Status.Retrying(backoff, ended.reason))
            try {
                Thread.sleep(backoff * 1000)
            } catch (e: InterruptedException) {
                break
            }
            backoff = minOf(backoff * 2, 30)
        }
        onStatus(Status.Stopped)
    }

    private class SessionEnd(val opened: Boolean, val code: Int, val reason: String)

    private fun session(): SessionEnd {
        val events = LinkedBlockingQueue<Event>()
        val ws = try {
            http.newWebSocket(Request.Builder().url(config.server).build(), object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    events.put(Event.Opened)
                }

                override fun onMessage(webSocket: WebSocket, text: String) {
                    handle(webSocket, Protocol.parse(text))
                }

                override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                    events.put(Event.Ended(code, reason.ifBlank { "closed by the brain" }))
                    webSocket.close(1000, null)
                }

                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    events.put(Event.Ended(-1, t.message ?: t.javaClass.simpleName))
                }
            })
        } catch (e: IllegalArgumentException) {
            return SessionEnd(false, -1, "bad server address")
        }
        socket = ws

        val first = try {
            events.take()
        } catch (e: InterruptedException) {
            ws.cancel()
            return SessionEnd(false, -1, "stopped")
        }
        if (first is Event.Ended) return SessionEnd(false, first.code, first.reason)

        log.log("connected to ${config.server} as ${config.id}", null)
        inConversation = false
        asleep = false // the brain says "sleep" right after hello if it is asleep
        onStatus(Status.Idle)
        ws.send(Protocol.hello(config.id, config.token))

        var end: Event.Ended? = null
        val mic = micFactory()
        try {
            mic.start()
            val buf = ByteArray(CHUNK_BYTES)
            while (running) {
                end = events.poll() as? Event.Ended
                if (end != null) break
                if (!mic.read(buf)) {
                    end = Event.Ended(-1, "microphone stopped")
                    break
                }
                if (!voice.muted && !ws.send(buf.toByteString())) {
                    // usually the brain closed the socket; wait for its close code (4001 = bad token)
                    end = events.poll(1, TimeUnit.SECONDS) as? Event.Ended ?: Event.Ended(-1, "send failed")
                    break
                }
            }
        } catch (e: InterruptedException) {
            // stop() was called
        } catch (e: Exception) {
            log.log("microphone failed", e)
            end = Event.Ended(-1, "microphone failed: ${e.message}")
        } finally {
            mic.stop()
        }
        if (end == null || end.code == -1) ws.cancel()
        return SessionEnd(true, end?.code ?: -1, end?.reason ?: "stopped")
    }

    private fun handle(ws: WebSocket, msg: BrainMessage) {
        when (msg) {
            BrainMessage.Wake -> {
                inConversation = true
                onStatus(Status.Listening)
                if (config.chime) {
                    voice.pause(350) // don't stream our own chime
                    chime.start()
                }
            }
            is BrainMessage.Say -> {
                onStatus(Status.Speaking)
                voice.speak(msg.text, msg.lang)
            }
            BrainMessage.TurnEnd -> voice.end {
                // bound to this socket: after a reconnect the brain isn't waiting for it
                ws.send(Protocol.speechDone())
                onStatus(if (inConversation) Status.Listening else resting())
            }
            is BrainMessage.Announce -> {
                onStatus(Status.Speaking)
                voice.speak(msg.text, msg.lang)
                voice.end { onStatus(if (inConversation) Status.Listening else resting()) }
            }
            BrainMessage.ConversationEnd -> {
                inConversation = false
                onStatus(resting())
                if (config.chime) chime.end()
            }
            BrainMessage.Sleep -> {
                asleep = true
                if (!inConversation) onStatus(Status.Sleeping)
            }
            BrainMessage.Awake -> {
                asleep = false
                if (!inConversation) onStatus(Status.Idle)
            }
            is BrainMessage.Unknown -> log.log("unknown message: ${msg.raw}", null)
        }
    }

    companion object {
        fun defaultHttpClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.SECONDS)
            .pingInterval(20, TimeUnit.SECONDS)
            .build()
    }
}
