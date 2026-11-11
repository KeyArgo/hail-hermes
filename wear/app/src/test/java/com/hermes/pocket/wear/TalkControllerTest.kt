package com.hermes.pocket.wear

import org.junit.Assert.*
import org.junit.Test

class TalkControllerTest {
    private class FakeTalkTransport(
        var audioStartSucceeds: Boolean = true,
        var audioEndSucceeds: Boolean = true
    ) : TalkTransport {
        var undeliveredCount = 0
        override fun onUndelivered() {
            undeliveredCount++
            events.add("undelivered")
        }
        val events = mutableListOf<String>()
        val sentFrames = mutableListOf<ByteArray>()
        var audioStartCount = 0
        var audioEndCount = 0
        var recorderStartCount = 0
        var recorderStopCount = 0

        override fun startRecorder(): Boolean {
            recorderStartCount++
            events.add("recorder_start")
            return true
        }

        override fun stopRecorder() {
            recorderStopCount++
            events.add("recorder_stop")
        }

        override fun sendAudioStart(): Boolean {
            audioStartCount++
            events.add("audio_start")
            return audioStartSucceeds
        }

        override fun sendAudioFrame(pcm: ByteArray): Boolean {
            sentFrames.add(pcm)
            events.add("audio_frame:${pcm.size}")
            return true
        }

        override fun sendAudioEnd(): Boolean {
            audioEndCount++
            events.add("audio_end")
            return audioEndSucceeds
        }
    }

    @Test
    fun holdWithNoConnectionNeverShowsSendingAndReportsUndelivered() {
        val transport = FakeTalkTransport(audioStartSucceeds = false)
        val controller = TalkController(transport)
        controller.startHold(nowMs = 1_000L)
        controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 1 }, nowMs = 1_400L)
        controller.endHold(nowMs = 1_500L)
        assertEquals(TalkState.IDLE, controller.state)
        assertEquals(1, transport.undeliveredCount)
        assertEquals(0, transport.audioEndCount)
    }

    @Test
    fun failedAudioEndNeverLeavesSendingStuck() {
        val transport = FakeTalkTransport(audioEndSucceeds = false)
        val controller = TalkController(transport)
        controller.startHold(nowMs = 1_000L)
        controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 1 }, nowMs = 1_400L)
        controller.endHold(nowMs = 1_500L)
        assertEquals(TalkState.IDLE, controller.state)
        assertEquals(1, transport.undeliveredCount)
    }

    @Test
    fun holdToTalkTransitionsIdleToListeningToSendingAndLocksScrollWhileListening() {
        val transport = FakeTalkTransport()
        val controller = TalkController(transport)

        assertEquals(TalkState.IDLE, controller.state)
        assertFalse(controller.isScrollLocked)

        controller.startHold(nowMs = 1_000L)
        assertEquals(TalkState.LISTENING, controller.state)
        assertTrue("List scrolling must be locked while listening", controller.isScrollLocked)
        assertEquals(1, transport.recorderStartCount)

        controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 1 }, nowMs = 1_100L)
        controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 2 }, nowMs = 1_350L)

        controller.endHold(nowMs = 1_450L)
        assertEquals(TalkState.SENDING, controller.state)
        assertFalse("List scrolling must unlock after listening stops", controller.isScrollLocked)
        assertEquals(1, transport.recorderStopCount)
        assertEquals(1, transport.audioStartCount)
        assertEquals(2, transport.sentFrames.size)
        assertEquals(1, transport.audioEndCount)

        controller.onServerState("thinking")
        assertEquals(TalkState.IDLE, controller.state)
    }

    @Test
    fun tapToStartAndTapToSendTransitionsIdleToListeningToSending() {
        val transport = FakeTalkTransport()
        val controller = TalkController(transport)

        controller.onTap(nowMs = 2_000L)
        assertEquals(TalkState.LISTENING, controller.state)
        assertTrue(controller.isScrollLocked)

        controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 7 }, nowMs = 2_200L)

        controller.onTap(nowMs = 2_500L)
        assertEquals(TalkState.SENDING, controller.state)
        assertFalse(controller.isScrollLocked)
        assertEquals(1, transport.audioStartCount)
        assertEquals(1, transport.sentFrames.size)
        assertEquals(1, transport.audioEndCount)
    }

    @Test
    fun utterancesShorterThan300MsAreIgnoredAndNotSent() {
        val holdTransport = FakeTalkTransport()
        val holdController = TalkController(holdTransport)

        holdController.startHold(nowMs = 5_000L)
        holdController.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 3 }, nowMs = 5_100L)
        holdController.endHold(nowMs = 5_299L)

        assertEquals(TalkState.IDLE, holdController.state)
        assertFalse(holdController.isScrollLocked)
        assertEquals(0, holdTransport.audioStartCount)
        assertEquals(0, holdTransport.sentFrames.size)
        assertEquals(0, holdTransport.audioEndCount)

        val tapTransport = FakeTalkTransport()
        val tapController = TalkController(tapTransport)

        tapController.onTap(nowMs = 10_000L)
        tapController.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 4 }, nowMs = 10_050L)
        tapController.onTap(nowMs = 10_250L)

        assertEquals(TalkState.IDLE, tapController.state)
        assertFalse(tapController.isScrollLocked)
        assertEquals(0, tapTransport.audioStartCount)
        assertEquals(0, tapTransport.sentFrames.size)
        assertEquals(0, tapTransport.audioEndCount)
    }

    @Test
    fun onPauseWithin300MsDoesNotSendAudioEnd() {
        val transport = FakeTalkTransport()
        val controller = TalkController(transport)

        controller.startHold(nowMs = 1_000L)
        controller.onPause(nowMs = 1_100L)

        assertEquals(TalkState.IDLE, controller.state)
        assertEquals(1, transport.recorderStopCount)
        assertEquals(0, transport.audioStartCount)
        assertEquals(0, transport.audioEndCount)
    }

    @Test
    fun onPauseWhileListeningWithStartedStreamSendsAudioEndOnce() {
        val transport = FakeTalkTransport()
        val controller = TalkController(transport)

        controller.startHold(nowMs = 1_000L)
        controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 0x10 }, nowMs = 1_350L)
        // Pause counts as the end of the utterance, so it must be past the bump threshold.
        controller.onPause(nowMs = 1_000L + TalkController.MIN_UTTERANCE_MS + 200)

        assertEquals(TalkState.SENDING, controller.state)
        assertFalse(controller.isScrollLocked)
        assertEquals(1, transport.recorderStopCount)
        assertEquals(1, transport.audioStartCount)
        assertEquals(1, transport.audioEndCount)

        controller.onPause(nowMs = 1_000L + TalkController.MIN_UTTERANCE_MS + 400)
        assertEquals(1, transport.audioEndCount)
    }

    @Test
    fun onPauseAfterBufferedFramesFlushesSpeechBeforeWireEnd() {
        val transport = FakeTalkTransport()
        val controller = TalkController(transport)

        controller.startHold(nowMs = 1_000L)
        controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 0x10 }, nowMs = 1_020L)
        controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 2 }, nowMs = 1_280L)
        controller.onPause(nowMs = 1_310L)

        assertEquals(TalkState.SENDING, controller.state)
        assertEquals(1, transport.audioStartCount)
        assertEquals(2, transport.sentFrames.size)
        assertEquals(1, transport.audioEndCount)
    }

    @Test
    fun onGesturePressWhileListeningDoesNotSignalStartBuzz() {
        val transport = FakeTalkTransport()
        val controller = TalkController(transport)

        assertTrue(controller.onGesturePress(nowMs = 1_000L))
        assertEquals(TalkState.LISTENING, controller.state)

        assertFalse(
            "Second press while listening must not trigger haptic-before-send",
            controller.onGesturePress(nowMs = 1_000L + TalkController.MIN_UTTERANCE_MS + 200)
        )
        assertEquals(TalkState.LISTENING, controller.state)

        controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 1 }, nowMs = 1_350L)
        controller.onGestureRelease(nowMs = 1_500L)
        assertEquals(TalkState.SENDING, controller.state)
    }

    @Test
    fun onGesturePressShortTapLatchesListening() {
        val transport = FakeTalkTransport()
        val controller = TalkController(transport)

        assertTrue(controller.onGesturePress(nowMs = 2_000L))
        controller.onGestureRelease(nowMs = 2_250L)
        assertEquals(TalkState.LISTENING, controller.state)
        assertTrue(controller.isScrollLocked)
        assertEquals(0, transport.recorderStopCount)
        assertEquals(0, transport.audioStartCount)
        assertEquals(0, transport.audioEndCount)
    }

    @Test
    fun pauseAfterSilenceDoesNotCommitTurn() {
        val transport = FakeTalkTransport()
        val controller = TalkController(transport)
        controller.startHold(1_000L)
        controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES), 1_400L)
        controller.onPause(1_500L)
        assertEquals(TalkState.IDLE, controller.state)
        assertEquals(0, transport.audioStartCount)
        assertEquals(0, transport.audioEndCount)
    }

    @Test
    fun cancelledHoldDiscardsEvenLongRecordedSpeech() {
        val transport = FakeTalkTransport()
        val controller = TalkController(transport)
        controller.onGesturePress(1_000L)
        controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 0x10 }, 1_400L)
        controller.onGestureCancel()
        assertEquals(TalkState.IDLE, controller.state)
        assertEquals(1, transport.recorderStopCount)
        assertEquals(0, transport.audioStartCount)
        assertEquals(0, transport.audioEndCount)
    }

    @Test
    fun tapListeningEndsOnSpeechThenSilence() {
        val transport = FakeTalkTransport()
        val controller = TalkController(transport)
        controller.onGesturePress(1_000L)
        controller.onTalkGestureRelease(1_100L)
        controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 0x10 }, 1_400L)
        // The quiet window ends the tap-utterance, so it has to exceed the silence threshold.
        controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES),
            1_400L + VoiceActivity.DEFAULT_END_OF_SPEECH_SILENCE_MS + 100)
        assertEquals(TalkState.SENDING, controller.state)
        assertEquals(1, transport.audioEndCount)
    }

    @Test
    fun failedAudioStartDoesNotMarkWireStartedOrSendOrphanEnd() {
        val transport = FakeTalkTransport(audioStartSucceeds = false)
        val controller = TalkController(transport)

        controller.startHold(nowMs = 1_000L)
        controller.onPcmFrame(ByteArray(Codec.FRAME_BYTES) { 1 }, nowMs = 1_350L)
        controller.endHold(nowMs = 1_450L)

        assertEquals(TalkState.IDLE, controller.state)
        // A failed start may be retried at the next step; what matters is no orphan end and no frames.
        assertTrue(transport.audioStartCount >= 1)
        assertEquals(0, transport.audioEndCount)
        assertEquals(0, transport.sentFrames.size)
    }
}
