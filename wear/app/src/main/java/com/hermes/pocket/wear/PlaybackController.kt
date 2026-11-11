package com.hermes.pocket.wear

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import java.util.Collections
import java.util.concurrent.atomic.AtomicInteger

interface AudioSink {
    fun open()
    fun write(pcm: ByteArray)
    fun flush()
    fun release()
}

class PlaybackController(
    private val sink: AudioSink,
    private val scope: CoroutineScope,
    channelCapacity: Int = DEFAULT_CHANNEL_CAPACITY,
    private val onStreamDrained: () -> Unit = {},
    /** Called after an interrupt stops playback, so "speaking" UI state can be cleared. */
    private val onInterrupted: () -> Unit = {}
) {
    private sealed class Command {
        data class Start(val streamId: Int) : Command()
        data class Frame(val streamId: Int, val pcm: ByteArray) : Command()
        data class End(val streamId: Int) : Command()
        data object Interrupt : Command()
    }

    /** Bounded queue between OkHttp callbacks and the consumer (backpressure, no drops). */
    private val channel = Channel<Command>(capacity = channelCapacity)
    /**
     * Unbounded ingress so callback threads never drop frames; a bridge coroutine applies backpressure
     * via [channel.send] and preserves ordering (including audio_end after all frames).
     */
    private val ingress = Channel<Command>(capacity = Channel.UNLIMITED)

    private val streamCounter = AtomicInteger(0)
    private val acceptingStreamId = AtomicInteger(0)
    /** All stream ids at or below this value are dropped by the consumer (interrupt barrier). */
    private val interruptedUpToStreamId = AtomicInteger(0)
    /** LIFO stack of streams that have started but not yet received audio_end (supports overlap). */
    private val streamsAwaitingEnd =
        Collections.synchronizedList(mutableListOf<Int>())
    private val interruptedStreams =
        Collections.synchronizedSet(mutableSetOf<Int>())

    private val bridgeJob: Job = scope.launch {
        for (cmd in ingress) {
            channel.send(cmd)
        }
    }

    private fun isStreamDropped(streamId: Int): Boolean {
        if (streamId <= 0) return false
        return streamId <= interruptedUpToStreamId.get() ||
            interruptedStreams.contains(streamId)
    }

    private val consumerJob: Job = scope.launch {
        var openedStreamId = -1
        var deferredStartId = -1
        val deferredFrames = mutableMapOf<Int, MutableList<ByteArray>>()
        val pendingEndStreamIds = mutableSetOf<Int>()

        fun openStream(streamId: Int) {
            if (isStreamDropped(streamId)) return
            sink.open()
            openedStreamId = streamId
        }

        fun releaseOpened() {
            if (openedStreamId != -1) {
                sink.release()
                openedStreamId = -1
            }
        }

        fun advanceToNextStream() {
            while (deferredStartId != -1) {
                val next = deferredStartId
                deferredStartId = -1
                if (isStreamDropped(next)) {
                    deferredFrames.remove(next)
                    pendingEndStreamIds.remove(next)
                    continue
                }
                openStream(next)
                deferredFrames.remove(next)?.forEach { sink.write(it) }
                if (pendingEndStreamIds.remove(next)) {
                    releaseOpened()
                    onStreamDrained()
                    continue
                }
                break
            }
        }

        /** AudioTrack can throw (device busy, focus lost); drop that stream so later replies still play. */
        fun dropAfterSinkFailure(streamId: Int) {
            if (streamId > 0) interruptedStreams.add(streamId)
            if (openedStreamId != -1) {
                interruptedStreams.add(openedStreamId)
                runCatching { sink.release() }
                openedStreamId = -1
            }
            deferredFrames.remove(streamId)
            pendingEndStreamIds.remove(streamId)
            onStreamDrained()
            runCatching { advanceToNextStream() }
        }

        try {
        for (cmd in channel) {
            try {
            when (cmd) {
                is Command.Start -> {
                    if (isStreamDropped(cmd.streamId)) continue
                    if (openedStreamId == -1) {
                        openStream(cmd.streamId)
                    } else if (openedStreamId != cmd.streamId) {
                        deferredStartId = cmd.streamId
                    }
                }
                is Command.Frame -> {
                    if (isStreamDropped(cmd.streamId)) continue
                    when {
                        openedStreamId == cmd.streamId -> sink.write(cmd.pcm)
                        openedStreamId == -1 -> openStream(cmd.streamId).also { sink.write(cmd.pcm) }
                        else -> deferredFrames.getOrPut(cmd.streamId) { mutableListOf() }.add(cmd.pcm)
                    }
                }
                is Command.End -> {
                    if (isStreamDropped(cmd.streamId)) {
                        deferredFrames.remove(cmd.streamId)
                        pendingEndStreamIds.remove(cmd.streamId)
                        if (openedStreamId == cmd.streamId) {
                            releaseOpened()
                        }
                        advanceToNextStream()
                        continue
                    }
                    pendingEndStreamIds.add(cmd.streamId)
                    if (openedStreamId == cmd.streamId) {
                        releaseOpened()
                        onStreamDrained()
                        advanceToNextStream()
                    }
                }
                is Command.Interrupt -> {
                    if (openedStreamId != -1) {
                        interruptedStreams.add(openedStreamId)
                    }
                    deferredStartId = -1
                    deferredFrames.clear()
                    pendingEndStreamIds.clear()
                    sink.flush()
                    releaseOpened()
                    onInterrupted()
                }
            }
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: RuntimeException) {
                val failedId = when (cmd) {
                    is Command.Start -> cmd.streamId
                    is Command.Frame -> cmd.streamId
                    is Command.End -> cmd.streamId
                    Command.Interrupt -> -1
                }
                dropAfterSinkFailure(failedId)
            }
        }
        } finally {
            // The service cancels its scope right after close(): never leak a playing track.
            if (openedStreamId != -1) {
                sink.flush()
                sink.release()
            }
        }
    }

    private fun enqueue(cmd: Command) {
        ingress.trySend(cmd)
    }

    fun onAudioStart() {
        val streamId = streamCounter.incrementAndGet()
        acceptingStreamId.set(streamId)
        synchronized(streamsAwaitingEnd) { streamsAwaitingEnd.add(streamId) }
        interruptedStreams.remove(streamId)
        enqueue(Command.Start(streamId))
    }

    fun onAudioFrame(pcm: ByteArray) {
        if (pcm.isEmpty()) return
        val streamId = acceptingStreamId.get()
        if (streamId == 0) return
        if (isStreamDropped(streamId)) return
        enqueue(Command.Frame(streamId, pcm))
    }

    fun onAudioEnd() {
        val streamId = synchronized(streamsAwaitingEnd) {
            if (streamsAwaitingEnd.isEmpty()) return
            streamsAwaitingEnd.removeAt(streamsAwaitingEnd.lastIndex)
        }
        if (acceptingStreamId.get() == streamId) {
            synchronized(streamsAwaitingEnd) {
                acceptingStreamId.set(streamsAwaitingEnd.lastOrNull() ?: 0)
            }
        }
        if (isStreamDropped(streamId)) return
        enqueue(Command.End(streamId))
    }

    fun interrupt() {
        val upTo = streamCounter.get()
        interruptedUpToStreamId.set(upTo)
        val acceptId = acceptingStreamId.getAndSet(0)
        if (acceptId != 0) {
            interruptedStreams.add(acceptId)
        }
        synchronized(streamsAwaitingEnd) {
            for (id in streamsAwaitingEnd) {
                interruptedStreams.add(id)
            }
            streamsAwaitingEnd.clear()
        }
        enqueue(Command.Interrupt)
    }

    @Suppress("UNUSED_PARAMETER")
    fun onStateChanged(screen: String) {
        // Protocol v2: state messages never stop or interrupt audio playback.
    }

    fun close() {
        interrupt()
        ingress.close()
        // Never block the caller (the service's onDestroy runs on the main thread): the consumer may be draining audio.
        scope.launch {
            bridgeJob.join()
            channel.close()
            consumerJob.join()
        }
    }

    companion object {
        const val DEFAULT_CHANNEL_CAPACITY = 256
    }
}

class AndroidAudioSink : AudioSink {
    private var track: AudioTrack? = null
    private var totalFramesWritten: Long = 0L
    @Volatile
    private var flushed: Boolean = false

    override fun open() {
        release()
        totalFramesWritten = 0L
        flushed = false
        val minSize = maxOf(
            Codec.FRAME_BYTES * 2,
            AudioTrack.getMinBufferSize(
                Codec.SAMPLE_RATE,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            )
        )
        track = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setSampleRate(Codec.SAMPLE_RATE)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .build()
            )
            .setTransferMode(AudioTrack.MODE_STREAM)
            .setBufferSizeInBytes(minSize * 2)
            .build()
            .also { it.play() }
    }

    override fun write(pcm: ByteArray) {
        val current = track ?: run {
            open()
            track
        } ?: return
        val written = current.write(pcm, 0, pcm.size, AudioTrack.WRITE_BLOCKING)
        if (written > 0) {
            totalFramesWritten += (written / 2)
        }
    }

    override fun flush() {
        flushed = true
        val current = track ?: return
        try {
            current.pause()
            current.flush()
        } catch (_: IllegalStateException) {
        }
    }

    override fun release() {
        val current = track ?: return
        try {
            if (!flushed && totalFramesWritten > 0L) {
                current.stop()
                val sampleRate = Codec.SAMPLE_RATE.toLong()
                val deadline = drainDeadlineMs(current, sampleRate)
                while (System.currentTimeMillis() < deadline) {
                    if (flushed) break
                    val head = current.playbackHeadPosition.toLong() and 0xFFFFFFFFL
                    if (head >= totalFramesWritten) break
                    Thread.sleep(5L)
                }
            } else {
                current.stop()
            }
        } catch (_: Exception) {
        } finally {
            track = null
            try {
                current.release()
            } catch (_: Exception) {
            }
        }
    }

    private fun drainDeadlineMs(track: AudioTrack, sampleRate: Long): Long {
        val head = track.playbackHeadPosition.toLong() and 0xFFFFFFFFL
        val framesRemaining = (totalFramesWritten - head).coerceAtLeast(0L)
        val audioMs = if (sampleRate > 0L) {
            (framesRemaining * 1000L + sampleRate - 1L) / sampleRate
        } else {
            30_000L
        }
        return System.currentTimeMillis() + maxOf(500L, audioMs + 500L)
    }
}
