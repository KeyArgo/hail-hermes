package com.hermes.pocket.wear

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import org.junit.Assert.*
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class PlaybackControllerTest {
    private class FakeAudioSink(
        private val onFrameWritten: ((Int) -> Unit)? = null
    ) : AudioSink {
        val events = mutableListOf<String>()
        val writtenFrames = mutableListOf<ByteArray>()
        var openCount = 0
        var flushCount = 0
        var releaseCount = 0
        var writtenCountAtRelease: Int? = null

        override fun open() {
            openCount++
            events.add("open")
        }

        override fun write(pcm: ByteArray) {
            writtenFrames.add(pcm)
            events.add("write:${writtenFrames.size}")
            onFrameWritten?.invoke(writtenFrames.size)
        }

        override fun flush() {
            flushCount++
            events.add("flush")
        }

        override fun release() {
            releaseCount++
            writtenCountAtRelease = writtenFrames.size
            events.add("release")
        }
    }

    @Test
    fun fiftyFramesPlusAudioEndPlusStateIdleWritesAll50FramesBeforeRelease() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        val scope = TestScope(dispatcher)
        val sink = FakeAudioSink()
        val controller = PlaybackController(sink = sink, scope = scope, channelCapacity = 64)

        controller.onAudioStart()
        for (i in 1..50) {
            controller.onAudioFrame(ByteArray(Codec.FRAME_BYTES) { i.toByte() })
        }
        controller.onAudioEnd()
        controller.onStateChanged("idle")

        scope.advanceUntilIdle()

        assertEquals(1, sink.openCount)
        assertEquals(50, sink.writtenFrames.size)
        assertEquals(0, sink.flushCount)
        assertEquals(1, sink.releaseCount)
        assertEquals(50, sink.writtenCountAtRelease)
        assertEquals("open", sink.events.first())
        assertEquals("release", sink.events.last())

        controller.close()
    }

    @Test
    fun interruptFromNonConsumerThreadDefersFlushToConsumer() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        val scope = TestScope(dispatcher)
        val sink = FakeAudioSink()
        val controller = PlaybackController(sink = sink, scope = scope, channelCapacity = 64)

        controller.onAudioStart()
        controller.onAudioFrame(ByteArray(Codec.FRAME_BYTES) { 1 })
        // Let the consumer open the stream first: a stream interrupted before it opens has nothing to release.
        scope.advanceUntilIdle()
        assertEquals(1, sink.openCount)
        assertEquals(0, sink.flushCount)

        controller.interrupt()
        assertEquals(0, sink.flushCount)

        scope.advanceUntilIdle()

        assertEquals(1, sink.flushCount)
        assertEquals(1, sink.releaseCount)

        controller.close()
    }

    @Test
    fun interruptAfter5FramesStopsWritingAndDropsQueuedAndLateFrames() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        val scope = TestScope(dispatcher)
        lateinit var controller: PlaybackController
        val sink = FakeAudioSink { count ->
            if (count == 5) {
                controller.interrupt()
            }
        }
        controller = PlaybackController(sink = sink, scope = scope, channelCapacity = 64)

        controller.onAudioStart()
        for (i in 1..20) {
            controller.onAudioFrame(ByteArray(Codec.FRAME_BYTES) { i.toByte() })
        }

        scope.advanceUntilIdle()

        for (i in 21..30) {
            controller.onAudioFrame(ByteArray(Codec.FRAME_BYTES) { i.toByte() })
        }
        controller.onAudioEnd()

        scope.advanceUntilIdle()

        assertEquals(1, sink.openCount)
        assertEquals(5, sink.writtenFrames.size)
        assertEquals(1, sink.flushCount)
        assertEquals(1, sink.releaseCount)

        controller.onAudioStart()
        controller.onAudioFrame(ByteArray(Codec.FRAME_BYTES) { 99 })
        controller.onAudioEnd()
        scope.advanceUntilIdle()

        assertEquals(2, sink.openCount)
        assertEquals(6, sink.writtenFrames.size)
        assertEquals(2, sink.releaseCount)

        controller.close()
    }

    @Test
    fun interruptAfterAudioEndStillDropsQueuedFramesWhenAcceptingStreamIdIsZero() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        val scope = TestScope(dispatcher)
        lateinit var controller: PlaybackController
        val sink = FakeAudioSink { count ->
            if (count == 5) {
                controller.interrupt()
            }
        }
        controller = PlaybackController(sink = sink, scope = scope, channelCapacity = 64)

        controller.onAudioStart()
        for (i in 1..50) {
            controller.onAudioFrame(ByteArray(Codec.FRAME_BYTES) { i.toByte() })
        }
        controller.onAudioEnd()

        scope.advanceUntilIdle()

        assertEquals(5, sink.writtenFrames.size)
        assertEquals(1, sink.flushCount)
        assertEquals(1, sink.releaseCount)

        controller.close()
    }

    @Test
    fun boundedChannelBackpressurePreservesAllFramesAndAudioEnd() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        val scope = TestScope(dispatcher)
        val sink = FakeAudioSink()
        val controller = PlaybackController(sink = sink, scope = scope, channelCapacity = 4)

        controller.onAudioStart()
        for (i in 1..100) {
            controller.onAudioFrame(ByteArray(Codec.FRAME_BYTES) { i.toByte() })
        }
        controller.onAudioEnd()

        scope.advanceUntilIdle()

        assertEquals(100, sink.writtenFrames.size)
        assertEquals(1, sink.releaseCount)
        assertEquals(100, sink.writtenCountAtRelease)

        controller.close()
    }

    @Test
    fun secondAudioStartWhileFirstStreamQueuedPlaysFirstStreamCompletely() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        val scope = TestScope(dispatcher)
        val sink = FakeAudioSink()
        val controller = PlaybackController(sink = sink, scope = scope, channelCapacity = 8)

        controller.onAudioStart()
        for (i in 1..10) {
            controller.onAudioFrame(ByteArray(Codec.FRAME_BYTES) { 1 })
        }
        controller.onAudioStart()
        for (i in 1..5) {
            controller.onAudioFrame(ByteArray(Codec.FRAME_BYTES) { 2 })
        }
        controller.onAudioEnd()
        controller.onAudioEnd()

        scope.advanceUntilIdle()

        assertEquals(10 + 5, sink.writtenFrames.size)
        assertEquals(2, sink.openCount)
        assertEquals(2, sink.releaseCount)

        controller.close()
    }

    @Test
    fun audioTrackFailureDropsThatReplyButTheNextReplyStillPlays() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        val scope = TestScope(dispatcher)
        var opens = 0
        val written = mutableListOf<ByteArray>()
        var drained = 0
        val sink = object : AudioSink {
            override fun open() {
                opens++
                if (opens == 1) throw IllegalStateException("AudioTrack busy")
            }
            override fun write(pcm: ByteArray) { written.add(pcm) }
            override fun flush() {}
            override fun release() {}
        }
        val controller = PlaybackController(sink = sink, scope = scope, channelCapacity = 64, onStreamDrained = { drained++ })

        controller.onAudioStart()
        controller.onAudioFrame(ByteArray(Codec.FRAME_BYTES) { 1 })
        controller.onAudioEnd()
        scope.advanceUntilIdle()
        controller.onAudioStart()
        controller.onAudioFrame(ByteArray(Codec.FRAME_BYTES) { 2 })
        controller.onAudioEnd()
        scope.advanceUntilIdle()

        assertEquals(1, written.size)
        assertEquals(2.toByte(), written[0][0])
        assertTrue(drained >= 2)

        controller.close()
    }

    @Test
    fun interruptReportsSoSpeakingStateCanBeCleared() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        val scope = TestScope(dispatcher)
        var interrupted = 0
        val controller = PlaybackController(
            sink = FakeAudioSink(), scope = scope, channelCapacity = 64, onInterrupted = { interrupted++ }
        )

        controller.onAudioStart()
        controller.onAudioFrame(ByteArray(Codec.FRAME_BYTES) { 1 })
        controller.interrupt()
        scope.advanceUntilIdle()

        assertEquals(1, interrupted)
        controller.close()
    }
}
