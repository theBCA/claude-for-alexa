package com.voiceagent.satellite

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

class VoiceTest {
    @Test
    fun speaksInOrderMutedUntilTheTailEnds() {
        val spoken = CopyOnWriteArrayList<String>()
        val voice = Voice({ text, lang -> spoken.add("$lang:$text"); Thread.sleep(30) }, tailMs = 100)
        val done = CountDownLatch(1)
        var mutedWhenDone = true

        voice.speak("one", "en")
        assertTrue(voice.muted)
        voice.speak("iki", "tr")
        voice.end { mutedWhenDone = voice.muted; done.countDown() }

        assertTrue(done.await(2, TimeUnit.SECONDS))
        assertEquals(listOf("en:one", "tr:iki"), spoken)
        assertFalse(mutedWhenDone)
        voice.shutdown()
    }

    @Test
    fun pauseMutesBriefly() {
        val voice = Voice({ _, _ -> }, tailMs = 0)
        voice.pause(150)
        assertTrue(voice.muted)
        Thread.sleep(400)
        assertFalse(voice.muted)
        voice.shutdown()
    }

    @Test
    fun aFailingTtsDoesNotStopTheQueue() {
        val done = CountDownLatch(1)
        val voice = Voice({ text, _ -> if (text == "bad") throw RuntimeException("engine died") }, tailMs = 0)
        voice.speak("bad", null)
        voice.speak("good", null)
        voice.end { done.countDown() }
        assertTrue(done.await(2, TimeUnit.SECONDS))
        assertFalse(voice.muted)
        voice.shutdown()
    }
}
