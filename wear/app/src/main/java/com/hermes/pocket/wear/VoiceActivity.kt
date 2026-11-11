package com.hermes.pocket.wear

import kotlin.math.sqrt

enum class VoiceDecision {
    CONTINUE,
    END_OF_SPEECH,
    SILENCE_TIMEOUT
}

enum class ConversationPhase {
    INACTIVE,
    LISTENING,
    WAITING_FOR_REPLY
}

class VoiceActivity(
    val energyThreshold: Int = DEFAULT_ENERGY_THRESHOLD,
    val endOfSpeechSilenceMs: Long = DEFAULT_END_OF_SPEECH_SILENCE_MS,
    @Volatile var maxSilenceMs: Long = DEFAULT_MAX_SILENCE_MS,
    val minUtteranceMs: Long = TalkController.MIN_UTTERANCE_MS
) {
    var phase: ConversationPhase = ConversationPhase.INACTIVE
        private set

    var hasDetectedSpeech: Boolean = false
        private set

    var listenStartedAtMs: Long = 0L
        private set

    var lastSpeechAtMs: Long = 0L
        private set

    private var replyStarted: Boolean = false
    private var audioPlaying: Boolean = false
    private var serverScreen: String = "idle"
    private var pendingGoodbyeStop: Boolean = false

    val isActive: Boolean
        @Synchronized get() = phase != ConversationPhase.INACTIVE

    val isReplyPending: Boolean
        @Synchronized get() = phase == ConversationPhase.WAITING_FOR_REPLY || audioPlaying

    @Synchronized
    fun hasTimedOut(nowMs: Long): Boolean {
        if (phase != ConversationPhase.LISTENING || hasDetectedSpeech) return false
        if (nowMs - listenStartedAtMs >= maxSilenceMs) {
            stopConversation()
            return true
        }
        return false
    }

    @Synchronized
    fun startConversation(nowMs: Long = 0L) {
        startListeningTurn(nowMs)
    }

    @Synchronized
    fun startListeningTurn(nowMs: Long = 0L) {
        phase = ConversationPhase.LISTENING
        hasDetectedSpeech = false
        listenStartedAtMs = nowMs
        lastSpeechAtMs = nowMs
        replyStarted = false
        audioPlaying = false
        pendingGoodbyeStop = false
    }

    @Synchronized
    fun stopConversation() {
        phase = ConversationPhase.INACTIVE
        hasDetectedSpeech = false
        replyStarted = false
        audioPlaying = false
        pendingGoodbyeStop = false
    }

    @Synchronized
    fun onTap(nowMs: Long = 0L): Boolean {
        return if (phase == ConversationPhase.INACTIVE) {
            startConversation(nowMs)
            true
        } else {
            stopConversation()
            false
        }
    }

    @Synchronized
    fun onPcmFrame(pcm: ByteArray, nowMs: Long): VoiceDecision {
        if (phase != ConversationPhase.LISTENING) return VoiceDecision.CONTINUE
        val energy = computeRmsEnergy(pcm)
        if (energy >= energyThreshold) {
            hasDetectedSpeech = true
            lastSpeechAtMs = nowMs
            return VoiceDecision.CONTINUE
        }

        if (hasDetectedSpeech) {
            val totalMs = nowMs - listenStartedAtMs
            val quietMs = nowMs - lastSpeechAtMs
            if (totalMs >= minUtteranceMs && quietMs >= endOfSpeechSilenceMs) {
                phase = ConversationPhase.WAITING_FOR_REPLY
                replyStarted = false
                audioPlaying = false
                return VoiceDecision.END_OF_SPEECH
            }
        } else if (nowMs - listenStartedAtMs >= maxSilenceMs) {
            stopConversation()
            return VoiceDecision.SILENCE_TIMEOUT
        }
        return VoiceDecision.CONTINUE
    }

    @Synchronized
    fun onReplyAudioStart() {
        if (phase != ConversationPhase.WAITING_FOR_REPLY) return
        replyStarted = true
        audioPlaying = true
    }

    @Synchronized
    fun onReplyAudioEnd(nowMs: Long = 0L): Boolean {
        if (phase == ConversationPhase.INACTIVE) return false
        replyStarted = true
        audioPlaying = false
        if (pendingGoodbyeStop) {
            stopConversation()
            return false
        }
        if (phase != ConversationPhase.WAITING_FOR_REPLY) return false
        startListeningTurn(nowMs)
        return true
    }

    @Synchronized
    fun onServerScreen(
        screen: String,
        nowMs: Long = 0L,
        conversationEnd: Boolean = false
    ): Boolean {
        val norm = Codec.normalizeScreen(screen)
        serverScreen = norm
        if (norm == "idle" && conversationEnd) {
            if (audioPlaying) {
                pendingGoodbyeStop = true
            } else {
                stopConversation()
            }
            return false
        }
        if (phase != ConversationPhase.WAITING_FOR_REPLY) return false
        if (norm == "thinking" || norm == "speaking") {
            replyStarted = true
            return false
        }
        if (norm == "idle" && replyStarted && !audioPlaying) {
            startListeningTurn(nowMs)
            return true
        }
        return false
    }

    companion object {
        const val DEFAULT_ENERGY_THRESHOLD = 350
        const val DEFAULT_END_OF_SPEECH_SILENCE_MS = 1_600L   // a breath mid-sentence is not the end
        const val DEFAULT_MAX_SILENCE_MS = 20_000L

        fun computeRmsEnergy(pcm: ByteArray): Int {
            val sampleCount = pcm.size / 2
            if (sampleCount <= 0) return 0
            var sumSquares = 0.0
            var i = 0
            while (i + 1 < pcm.size) {
                val lo = pcm[i].toInt() and 0xFF
                val hi = pcm[i + 1].toInt()
                val sample = ((hi shl 8) or lo).toShort().toInt()
                sumSquares += sample.toDouble() * sample.toDouble()
                i += 2
            }
            return sqrt(sumSquares / sampleCount).toInt()
        }

        fun isSpeechFrame(pcm: ByteArray, threshold: Int = DEFAULT_ENERGY_THRESHOLD): Boolean =
            computeRmsEnergy(pcm) >= threshold
    }
}
