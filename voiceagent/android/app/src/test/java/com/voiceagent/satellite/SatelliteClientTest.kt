package com.voiceagent.satellite

import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.ByteString
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** The client against a real websocket server playing the brain's part. */
class SatelliteClientTest {
    private lateinit var server: MockWebServer
    private val statuses = CopyOnWriteArrayList<Status>()
    private val spoken = CopyOnWriteArrayList<Pair<String, String?>>()
    private val chimes = AtomicInteger()
    private var client: SatelliteClient? = null
    private var voice: Voice? = null

    /** The brain's end of one connection. */
    private class Brain(private val onHello: (WebSocket) -> Unit = {}) : WebSocketListener() {
        val texts = LinkedBlockingQueue<String>()
        val frames = AtomicInteger()
        val frameSizes = CopyOnWriteArrayList<Int>()
        val opened = CountDownLatch(1)

        @Volatile
        var ws: WebSocket? = null

        override fun onOpen(webSocket: WebSocket, response: Response) {
            ws = webSocket
            opened.countDown()
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            texts.put(text)
            if (JSONObject(text).getString("type") == "hello") onHello(webSocket)
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(1000, null) // what a real server does
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            frames.incrementAndGet()
            frameSizes.add(bytes.size)
        }

        fun send(json: String) = ws!!.send(json)
        fun next(): JSONObject? = texts.poll(5, TimeUnit.SECONDS)?.let(::JSONObject)
    }

    private class FakeMic : Mic {
        override fun start() {}

        override fun read(buf: ByteArray): Boolean {
            Thread.sleep(10)
            return true
        }

        override fun stop() {}
    }

    private var framesDuringSpeech = -1

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @After
    fun tearDown() {
        client?.stop()
        voice?.shutdown()
        server.shutdown()
    }

    private fun startClient(brainFrames: () -> Int = { 0 }, chime: Boolean = true) {
        val tts = Tts { text, lang ->
            spoken.add(text to lang)
            // audio must not reach the brain while we speak
            Thread.sleep(50)
            val before = brainFrames()
            Thread.sleep(200)
            framesDuringSpeech = maxOf(framesDuringSpeech, brainFrames() - before)
        }
        val voice = Voice(tts, tailMs = 50).also { this.voice = it }
        val url = server.url("/").toString().replaceFirst("http", "ws")
        client = SatelliteClient(
            config = SatelliteConfig(server = url, id = "phone", token = "secret", chime = chime),
            micFactory = { FakeMic() },
            voice = voice,
            chime = object : Chime {
                override fun start() { chimes.incrementAndGet() }
                override fun end() {}
            },
            onStatus = { statuses.add(it) },
        ).also { it.start() }
    }

    private fun waitFor(what: String, condition: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 5000
        while (!condition()) {
            if (System.currentTimeMillis() > deadline) throw AssertionError("timed out waiting for $what")
            Thread.sleep(10)
        }
    }

    @Test
    fun fullConversation() {
        val brain = Brain()
        server.enqueue(MockResponse().withWebSocketUpgrade(brain))
        startClient(brainFrames = { brain.frames.get() })

        val hello = brain.next()!!
        assertEquals("hello", hello.getString("type"))
        assertEquals("phone", hello.getString("id"))
        assertEquals("secret", hello.getString("token"))

        waitFor("mic audio") { brain.frames.get() > 3 }
        assertTrue(brain.frameSizes.all { it == CHUNK_BYTES })

        brain.send("""{"type":"wake"}""")
        brain.send("""{"type":"say","text":"Tamam.","lang":"tr"}""")
        brain.send("""{"type":"say","text":"Işıkları sıcak yaptım.","lang":"tr"}""")
        brain.send("""{"type":"turn_end"}""")

        assertEquals("speech_done", brain.next()!!.getString("type"))
        assertEquals(listOf("Tamam." to "tr", "Işıkları sıcak yaptım." to "tr"), spoken)
        assertEquals(1, chimes.get())
        assertEquals(0, framesDuringSpeech)
        assertTrue(statuses.containsAll(listOf(Status.Idle, Status.Listening, Status.Speaking)))

        // after speaking, audio flows again for the follow-up window
        val resumed = brain.frames.get()
        waitFor("audio after speech") { brain.frames.get() > resumed + 3 }

        brain.send("""{"type":"conversation_end"}""")
        waitFor("idle") { statuses.last() == Status.Idle }
    }

    @Test
    fun announcementIsSpokenWithoutSpeechDone() {
        val brain = Brain()
        server.enqueue(MockResponse().withWebSocketUpgrade(brain))
        startClient()
        brain.next() // hello
        brain.send("""{"type":"announce","text":"Your tea is done.","lang":null}""")
        waitFor("announcement") { spoken.isNotEmpty() }
        assertEquals("Your tea is done." to null, spoken[0])
        assertNull(brain.texts.poll(700, TimeUnit.MILLISECONDS))
    }

    @Test
    fun wrongTokenStopsWithoutRetrying() {
        server.enqueue(MockResponse().withWebSocketUpgrade(Brain { it.close(Protocol.CLOSE_UNAUTHORIZED, "unauthorized") }))
        server.enqueue(MockResponse().withWebSocketUpgrade(Brain()))
        startClient()
        waitFor("rejection") { Status.Rejected in statuses }
        Thread.sleep(1500)
        assertEquals(1, server.requestCount)
        assertTrue(statuses.none { it is Status.Retrying })
    }

    @Test
    fun reconnectsAfterTheBrainDrops() {
        val first = Brain { it.close(1001, "restarting") }
        val second = Brain()
        server.enqueue(MockResponse().withWebSocketUpgrade(first))
        server.enqueue(MockResponse().withWebSocketUpgrade(second))
        startClient()
        assertEquals("hello", first.next()!!.getString("type"))
        assertEquals("hello", second.next()!!.getString("type"))
        assertTrue(statuses.any { it is Status.Retrying && it.seconds == 1L })
        waitFor("audio on the new connection") { second.frames.get() > 0 }
    }

    @Test
    fun stopEndsTheLoop() {
        val brain = Brain()
        server.enqueue(MockResponse().withWebSocketUpgrade(brain))
        startClient()
        brain.next()
        client!!.stop()
        client = null
        assertEquals(Status.Stopped, statuses.last())
    }
}
