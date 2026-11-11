package com.hermes.pocket.wear

import org.junit.Assert.*
import org.junit.Test

class VoiceActivityTest {
    private fun pcmFrameWithAmplitude(amplitude: Short, samples: Int = 320): ByteArray {
        val out = ByteArray(samples * 2)
        val lo = (amplitude.toInt() and 0xFF).toByte()
        val hi = ((amplitude.toInt() shr 8) and 0xFF).toByte()
        for (i in 0 until samples) {
            out[i * 2] = lo
            out[i * 2 + 1] = hi
        }
        return out
    }

    @Test
    fun detectsEndOfSpeechAfterOneSecondOfQuiet() {
        val vad = VoiceActivity()
        val silent = pcmFrameWithAmplitude(0)
        val speech = pcmFrameWithAmplitude(2500)

        assertTrue(vad.onTap(0L))
        assertEquals(ConversationPhase.LISTENING, vad.phase)

        assertEquals(VoiceDecision.CONTINUE, vad.onPcmFrame(speech, 100L))
        assertEquals(VoiceDecision.CONTINUE, vad.onPcmFrame(speech, 400L))
        assertTrue(vad.hasDetectedSpeech)

        assertEquals(VoiceDecision.CONTINUE,
            vad.onPcmFrame(silent, 400L + VoiceActivity.DEFAULT_END_OF_SPEECH_SILENCE_MS - 1))
        assertEquals(VoiceDecision.END_OF_SPEECH,
            vad.onPcmFrame(silent, 400L + VoiceActivity.DEFAULT_END_OF_SPEECH_SILENCE_MS + 1))
        assertEquals(ConversationPhase.WAITING_FOR_REPLY, vad.phase)
    }

    @Test
    fun resumesListeningAfterReplyAudioFinishes() {
        val vad = VoiceActivity()
        val speech = pcmFrameWithAmplitude(2500)
        val silent = pcmFrameWithAmplitude(0)

        vad.startConversation(0L)
        vad.onPcmFrame(speech, 500L)
        assertEquals(VoiceDecision.END_OF_SPEECH,
            vad.onPcmFrame(silent, 500L + VoiceActivity.DEFAULT_END_OF_SPEECH_SILENCE_MS))

        assertFalse(vad.onServerScreen("thinking", 1_600L))
        assertFalse(vad.onServerScreen("speaking", 1_700L))
        vad.onReplyAudioStart()
        assertFalse(vad.onServerScreen("idle", 2_000L))

        assertTrue(vad.onReplyAudioEnd(2_200L))
        assertEquals(ConversationPhase.LISTENING, vad.phase)
        assertFalse(vad.hasDetectedSpeech)
    }

    @Test
    fun endsAfterTwentySecondsOfSilenceOrTap() {
        val vad = VoiceActivity()
        val silent = pcmFrameWithAmplitude(20)

        vad.startConversation(1_000L)
        assertEquals(VoiceDecision.CONTINUE, vad.onPcmFrame(silent, 20_999L))
        assertTrue(vad.isActive)

        assertEquals(VoiceDecision.SILENCE_TIMEOUT, vad.onPcmFrame(silent, 21_000L))
        assertFalse(vad.isActive)
        assertEquals(ConversationPhase.INACTIVE, vad.phase)

        assertTrue(vad.onTap(30_000L))
        assertTrue(vad.isActive)
        assertFalse(vad.onTap(31_000L))
        assertFalse(vad.isActive)
    }
}
