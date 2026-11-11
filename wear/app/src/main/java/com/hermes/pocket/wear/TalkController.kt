package com.hermes.pocket.wear

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull

enum class TalkState {
    IDLE,
    LISTENING,
    SENDING
}

enum class GestureAction {
    NONE,
    START_HOLD,
    END_HOLD,
    START_CONVERSATION,
    STOP_CONVERSATION,
    INTERRUPT
}

interface TalkTransport {
    fun startRecorder(): Boolean
    fun stopRecorder()
    fun sendAudioStart(): Boolean
    fun sendAudioFrame(pcm: ByteArray): Boolean
    fun sendAudioEnd(): Boolean
    /** The utterance could not reach the hub (no connection): tell the user instead of dropping it silently. */
    fun onUndelivered() {}
}

class TalkController(
    private val transport: TalkTransport,
    private val onStateChanged: (TalkState) -> Unit = {}
) {
    var state: TalkState = TalkState.IDLE
        private set

    val isScrollLocked: Boolean
        get() = state == TalkState.LISTENING

    private var startedAtMs: Long = 0L
    private var audioStartedOnWire: Boolean = false
    private var lastShortTapReleaseAtMs: Long = -10_000L
    private val bufferedFrames = mutableListOf<ByteArray>()
    private val tapVoiceActivity = VoiceActivity()
    private var tapListening = false
    private var speechDetected = false
    private var gestureStartedRecorder = false
    private var gesturePressedAtMs = 0L

    @Synchronized
    fun startHold(nowMs: Long = System.currentTimeMillis()): Boolean {
        if (state == TalkState.LISTENING) return false
        if (!transport.startRecorder()) {
            updateState(TalkState.IDLE)
            return false
        }
        startedAtMs = nowMs
        audioStartedOnWire = false
        bufferedFrames.clear()
        speechDetected = false
        tapListening = false
        tapVoiceActivity.startListeningTurn(nowMs)
        updateState(TalkState.LISTENING)
        return true
    }

    @Synchronized
    fun endHold(nowMs: Long = System.currentTimeMillis()) {
        if (state != TalkState.LISTENING) return
        finishListening(nowMs)
    }

    @Synchronized
    fun onTap(nowMs: Long = System.currentTimeMillis()) {
        if (state == TalkState.LISTENING) {
            finishListening(nowMs)
        } else {
            startHold(nowMs)
        }
    }

    @Synchronized
    fun onGesturePress(nowMs: Long = System.currentTimeMillis()): Boolean {
        gesturePressedAtMs = nowMs
        gestureStartedRecorder = state != TalkState.LISTENING && startHold(nowMs)
        return gestureStartedRecorder
    }

    @Synchronized
    fun onGestureRelease(nowMs: Long = System.currentTimeMillis()) {
        onTalkGestureRelease(nowMs)
    }

    @Synchronized
    fun onGestureCancel() {
        if (gestureStartedRecorder) reset()
        gestureStartedRecorder = false
        lastShortTapReleaseAtMs = -10_000L
    }

    @Synchronized
    fun onTalkGestureRelease(
        nowMs: Long,
        serverScreen: String = "idle",
        conversationActive: Boolean = false
    ): GestureAction {
        val heldMs = nowMs - gesturePressedAtMs
        if (conversationActive) {
            reset()
            lastShortTapReleaseAtMs = -10_000L
            return GestureAction.STOP_CONVERSATION
        }
        if (heldMs >= MIN_UTTERANCE_MS) {
            if (state == TalkState.LISTENING) finishListening(nowMs)
            lastShortTapReleaseAtMs = -10_000L
            gestureStartedRecorder = false
            return GestureAction.END_HOLD
        }
        // A double tap used to switch into conversation mode, which is why the same button
        // sometimes read "Conv · Stop" and sometimes "Hold to talk" with no explanation.
        // Conversation is now only ever started from the Conversation pill.
        if (gestureStartedRecorder) {
            tapListening = true
            lastShortTapReleaseAtMs = nowMs
        } else if (state == TalkState.LISTENING) {
            onTap(nowMs)
            lastShortTapReleaseAtMs = -10_000L
        }
        gestureStartedRecorder = false
        return GestureAction.NONE
    }

    @Synchronized
    fun onPcmFrame(pcm: ByteArray, nowMs: Long = System.currentTimeMillis()) {
        if (state != TalkState.LISTENING) return
        val evenLen = pcm.size and 1.inv()
        if (evenLen <= 0) return
        val frame = if (evenLen == pcm.size) pcm else pcm.copyOf(evenLen)
        // Keep the turn local until release so a cancelled gesture cannot commit it.
        bufferedFrames.add(frame.copyOf())
        if (VoiceActivity.isSpeechFrame(frame)) speechDetected = true
        val decision = tapVoiceActivity.onPcmFrame(frame, nowMs)
        if (tapListening) {
            when (decision) {
                VoiceDecision.END_OF_SPEECH -> finishListening(nowMs)
                VoiceDecision.SILENCE_TIMEOUT -> reset()
                VoiceDecision.CONTINUE -> Unit
            }
        }
    }

    @Synchronized
    fun onPause(nowMs: Long = System.currentTimeMillis()) {
        if (state != TalkState.LISTENING) return
        if (speechDetected) finishListening(nowMs) else reset()
    }

    @Synchronized
    fun onServerState(screen: String) {
        if (state == TalkState.SENDING && screen != "listening") {
            updateState(TalkState.IDLE)
        }
    }

    @Synchronized
    fun reset() {
        if (state == TalkState.LISTENING) {
            transport.stopRecorder()
        }
        bufferedFrames.clear()
        audioStartedOnWire = false
        tapListening = false
        tapVoiceActivity.stopConversation()
        gestureStartedRecorder = false
        updateState(TalkState.IDLE)
    }

    private fun finishListening(nowMs: Long) {
        tapListening = false
        tapVoiceActivity.stopConversation()
        transport.stopRecorder()
        val durationMs = nowMs - startedAtMs
        if (durationMs < MIN_UTTERANCE_MS) {
            bufferedFrames.clear()
            audioStartedOnWire = false
            updateState(TalkState.IDLE)
            return
        }
        flushWireStartIfNeeded()
        if (audioStartedOnWire && transport.sendAudioEnd()) {
            updateState(TalkState.SENDING)
        } else {
            audioStartedOnWire = false
            bufferedFrames.clear()
            updateState(TalkState.IDLE)
            transport.onUndelivered()
        }
    }

    private fun flushWireStartIfNeeded() {
        if (audioStartedOnWire) return
        if (!transport.sendAudioStart()) return
        audioStartedOnWire = true
        for (frame in bufferedFrames) {
            transport.sendAudioFrame(frame)
        }
        bufferedFrames.clear()
    }

    private fun updateState(newState: TalkState) {
        state = newState
        onStateChanged(newState)
    }

    companion object {
        const val MIN_UTTERANCE_MS = 300L
        const val DOUBLE_TAP_WINDOW_MS = 380L
    }
}

class ActivityMicRecorder(
    private val scope: CoroutineScope,
    private val onFrame: (ByteArray) -> Unit,
    private val onPermissionDenied: () -> Unit = {}
) {
    private var record: AudioRecord? = null
    private var recordingJob: Job? = null

    @Synchronized
    fun start(): Boolean {
        if (recordingJob != null) return true
        // A recorder from the previous press may still be shutting down and holding the
        // microphone; starting another one fails and the press does nothing at all.
        releaseNow()
        return try {
            val minBuf = maxOf(
                Codec.FRAME_BYTES,
                AudioRecord.getMinBufferSize(
                    Codec.SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT
                )
            )
            var r: AudioRecord? = null
            // The microphone can take a moment to come free after a reply; one failed
            // initialize used to mean the press silently did nothing.
            for (attempt in 0 until 3) {
                val candidate = AudioRecord(
                    MediaRecorder.AudioSource.MIC,
                    Codec.SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    minBuf * 2
                )
                if (candidate.state == AudioRecord.STATE_INITIALIZED) {
                    r = candidate
                    break
                }
                candidate.release()
                Thread.sleep(60)
            }
            val record = r ?: return false
            record.startRecording()
            this.record = record
            recordingJob = scope.launch(Dispatchers.IO) {
                val buffer = ByteArray(Codec.FRAME_BYTES)
                try {
                    while (isActive) {
                        val n = record.read(buffer, 0, buffer.size)
                        if (!isActive) break
                        if (n > 0) {
                            val evenLen = n and 1.inv()
                            if (evenLen > 0 && isActive) {
                                onFrame(buffer.copyOf(evenLen))
                            }
                        } else if (n < 0) {
                            break
                        }
                    }
                } finally {
                    try {
                        r.stop()
                    } catch (_: IllegalStateException) {
                    }
                    try {
                        record.release()
                    } catch (_: Exception) {
                    }
                }
            }
            true
        } catch (_: SecurityException) {
            onPermissionDenied()
            false
        }
    }

    @Synchronized
    fun stop() {
        releaseNow()
    }

    /** Stop and release the recorder, waiting for the reader job so the mic is free. */
    private fun releaseNow() {
        val job = recordingJob
        val r = record
        recordingJob = null
        record = null
        try {
            r?.stop()
        } catch (_: IllegalStateException) {
        }
        job?.cancel()
        if (job != null) {
            // Bounded: never block the tap for long, but do wait for the release.
            runBlocking { withTimeoutOrNull(400) { job.join() } }
        }
        try {
            r?.release()
        } catch (_: Exception) {
        }
    }
}
