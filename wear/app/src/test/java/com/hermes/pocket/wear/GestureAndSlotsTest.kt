package com.hermes.pocket.wear

import org.junit.Assert.*
import org.junit.Test

class GestureAndSlotsTest {
    private class FakeTransport : TalkTransport {
        var recorderStartCount = 0
        var recorderStopCount = 0
        var audioStartCount = 0
        var audioEndCount = 0
        val frames = mutableListOf<ByteArray>()

        override fun startRecorder(): Boolean {
            recorderStartCount++
            return true
        }

        override fun stopRecorder() {
            recorderStopCount++
        }

        override fun sendAudioStart(): Boolean {
            audioStartCount++
            return true
        }

        override fun sendAudioFrame(pcm: ByteArray): Boolean {
            frames.add(pcm)
            return true
        }

        override fun sendAudioEnd(): Boolean {
            audioEndCount++
            return true
        }
    }

    @Test
    fun holdAndReleaseSendsOnReleaseAndNeverLatchesTapToSend() {
        val transport = FakeTransport()
        val controller = TalkController(transport)

        // Hold long enough to count as a sentence: MIN_UTTERANCE_MS guards against bumps.
        val press = 1_000L
        val release = press + TalkController.MIN_UTTERANCE_MS + 150
        assertTrue(controller.onGesturePress(press))
        assertEquals(TalkState.LISTENING, controller.state)
        controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 2 }, press + 350)

        val action = controller.onTalkGestureRelease(
            nowMs = release,
            serverScreen = "idle",
            conversationActive = false
        )
        assertEquals(GestureAction.END_HOLD, action)
        assertEquals(TalkState.SENDING, controller.state)
        assertEquals(1, transport.audioStartCount)
        assertEquals(1, transport.audioEndCount)
    }

    @Test
    fun tapsToggleListeningEvenWhileThinkingOrSpeaking() {
        for (screen in listOf("idle", "thinking", "speaking")) {
            val transport = FakeTransport()
            val controller = TalkController(transport)
            assertTrue(controller.onGesturePress(1_000L))
            assertEquals(GestureAction.NONE, controller.onTalkGestureRelease(1_100L, screen))
            assertEquals(TalkState.LISTENING, controller.state)
            controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 0x10 }, 1_500L)
            assertFalse(controller.onGesturePress(1_600L))
            assertEquals(GestureAction.NONE, controller.onTalkGestureRelease(1_700L, screen))
            assertEquals(TalkState.SENDING, controller.state)
            assertEquals(1, transport.audioEndCount)
        }
    }

    @Test
    fun doubleTapNoLongerSwitchesModeByItself() {
        // The button used to read "Conv · Stop" after an accidental double tap, which looked
        // like the app changing its mind. Conversation starts only from the Conversation pill.
        val transport = FakeTransport()
        val controller = TalkController(transport)
        assertTrue(controller.onGesturePress(1_000L))
        assertEquals(GestureAction.NONE, controller.onTalkGestureRelease(1_100L))
        assertFalse(controller.onGesturePress(1_280L))
        assertEquals(GestureAction.NONE, controller.onTalkGestureRelease(1_360L))
        assertEquals(1, transport.recorderStartCount)
        assertEquals(GestureAction.STOP_CONVERSATION,
            controller.onTalkGestureRelease(2_000L, conversationActive = true))
        assertEquals(TalkState.IDLE, controller.state)
    }

    @Test
    fun conversationLoopRepeatsAcrossTurnsAndStopsOnSilenceTapOrGoodbye() {
        val va = VoiceActivity(
            energyThreshold = 300,
            endOfSpeechSilenceMs = 1_000L,
            maxSilenceMs = 20_000L,
            minUtteranceMs = 300L
        )
        val loudFrame = ByteArray(Codec.FRAME_BYTES) { if (it % 2 == 0) 0x00 else 0x10 }
        val quietFrame = ByteArray(Codec.FRAME_BYTES) { 0 }

        va.startConversation(nowMs = 0L)
        assertTrue(va.isActive)

        // Turn 1: user speaks, pauses -> END_OF_SPEECH -> hub replies -> audio drains -> listens again
        assertEquals(VoiceDecision.CONTINUE, va.onPcmFrame(loudFrame, 200L))
        assertEquals(VoiceDecision.CONTINUE, va.onPcmFrame(loudFrame, 400L))
        assertEquals(VoiceDecision.END_OF_SPEECH, va.onPcmFrame(quietFrame, 1_500L))
        assertEquals(ConversationPhase.WAITING_FOR_REPLY, va.phase)

        va.onReplyAudioStart()
        // Server state idle arriving before audio finishes draining must not cut off or prematurely restart
        assertFalse(va.onServerScreen("idle", nowMs = 2_000L, conversationEnd = false))
        // When audio finishes draining, automatically listen again for Turn 2
        assertTrue(va.onReplyAudioEnd(nowMs = 2_500L))
        assertEquals(ConversationPhase.LISTENING, va.phase)
        assertTrue(va.isActive)

        // Turn 2: user says goodbye -> hub sends state idle with conversation_end:true
        assertEquals(VoiceDecision.CONTINUE, va.onPcmFrame(loudFrame, 2_900L))
        assertEquals(VoiceDecision.END_OF_SPEECH, va.onPcmFrame(quietFrame, 4_000L))
        va.onReplyAudioStart()
        assertFalse(va.onServerScreen("idle", nowMs = 4_500L, conversationEnd = true))
        assertFalse("Must stop after goodbye audio finishes draining", va.onReplyAudioEnd(nowMs = 5_000L))
        assertFalse(va.isActive)

        // Silence timeout rule (e.g. 20s of silence)
        va.startConversation(nowMs = 10_000L)
        assertEquals(VoiceDecision.CONTINUE, va.onPcmFrame(quietFrame, 29_999L))
        assertEquals(VoiceDecision.SILENCE_TIMEOUT, va.onPcmFrame(quietFrame, 30_000L))
        assertFalse(va.isActive)
    }

    @Test
    fun codecParsesSlotsAndCyclesQuickThroughThreeSlotsAndBack() {
        val raw = """
            {
              "v": 2,
              "type": "slots",
              "items": [
                {"slot": 2, "id": "sess-2", "title": "Project Beta"},
                {"slot": 1, "id": "sess-1", "title": "Daily Notes"},
                {"slot": 3, "id": "sess-3", "title": "Server Ops"}
              ]
            }
        """.trimIndent()
        val obj = Codec.decode(raw)
        assertNotNull(obj)
        val parsed = Codec.parseSlots(obj!!)
        assertNotNull(parsed)
        assertEquals(3, parsed!!.items.size)
        assertEquals(1, parsed.items[0].slot)
        assertEquals("sess-1", parsed.items[0].id)
        assertEquals("Daily Notes", parsed.items[0].title)

        var mode = Codec.ModeMessage(mode = Codec.MODE_QUICK)
        mode = Codec.cycleSlotMode(mode, parsed.items, direction = 1)
        assertEquals(Codec.MODE_SESSION, mode.mode)
        assertEquals("sess-1", mode.sessionId)
        assertEquals("Daily Notes", mode.displayLabel)

        mode = Codec.cycleSlotMode(mode, parsed.items, direction = 1)
        assertEquals("sess-2", mode.sessionId)

        mode = Codec.cycleSlotMode(mode, parsed.items, direction = 1)
        assertEquals("sess-3", mode.sessionId)

        mode = Codec.cycleSlotMode(mode, parsed.items, direction = 1)
        assertEquals(Codec.MODE_QUICK, mode.mode)
        assertEquals("Quick ask", mode.displayLabel)

        // Reverse swipe wraps Quick ask -> slot 3
        mode = Codec.cycleSlotMode(mode, parsed.items, direction = -1)
        assertEquals("sess-3", mode.sessionId)
    }
}
