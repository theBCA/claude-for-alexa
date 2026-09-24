package com.voiceagent.satellite

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ProtocolTest {
    @Test
    fun parsesEveryBrainMessage() {
        assertEquals(BrainMessage.Wake, Protocol.parse("""{"type":"wake"}"""))
        assertEquals(BrainMessage.Say("Tamam.", "tr"), Protocol.parse("""{"type":"say","text":"Tamam.","lang":"tr"}"""))
        assertEquals(BrainMessage.TurnEnd, Protocol.parse("""{"type":"turn_end"}"""))
        assertEquals(BrainMessage.ConversationEnd, Protocol.parse("""{"type":"conversation_end"}"""))
        assertEquals(
            BrainMessage.Announce("Your tea is done.", "en"),
            Protocol.parse("""{"type":"announce","text":"Your tea is done.","lang":"en"}"""))
    }

    @Test
    fun nullLanguageStaysNull() {
        // the brain sends "lang": null for an announcement before anyone has spoken
        assertEquals(BrainMessage.Announce("Timer", null), Protocol.parse("""{"type":"announce","text":"Timer","lang":null}"""))
        assertEquals(BrainMessage.Say("Hi", null), Protocol.parse("""{"type":"say","text":"Hi"}"""))
    }

    @Test
    fun keepsNonAsciiText() {
        val msg = Protocol.parse("""{"type":"say","text":"Işıkları sıcak yaptım.","lang":"tr"}""")
        assertEquals("Işıkları sıcak yaptım.", (msg as BrainMessage.Say).text)
    }

    @Test
    fun unknownAndBrokenMessagesDontThrow() {
        assertTrue(Protocol.parse("""{"type":"something_new"}""") is BrainMessage.Unknown)
        assertTrue(Protocol.parse("not json") is BrainMessage.Unknown)
    }

    @Test
    fun helloAndSpeechDoneMatchServerPy() {
        val hello = JSONObject(Protocol.hello("living-room", "secret"))
        assertEquals("hello", hello.getString("type"))
        assertEquals("living-room", hello.getString("id"))
        assertEquals("secret", hello.getString("token"))
        assertEquals("speech_done", JSONObject(Protocol.speechDone()).getString("type"))
    }
}
