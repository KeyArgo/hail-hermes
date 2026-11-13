package com.hermes.pocket.wear

import org.junit.Assert.*
import org.junit.Test

class ConnectionTrackerTest {
    @Test
    fun oldSocketCallbacksCannotMutateStateOfNewSocketGeneration() {
        val tracker = ConnectionTracker()
        var status = "Disconnected"

        val gen1 = tracker.nextGeneration()
        assertTrue(tracker.onSocketOpened(gen1))
        tracker.runIfCurrent(gen1) { status = "Connected-1" }
        assertEquals("Connected-1", status)

        // Start a second connection (e.g., settings change or reconnect)
        val gen2 = tracker.nextGeneration()
        assertNotEquals(gen1, gen2)
        assertFalse(tracker.isCurrent(gen1))
        assertTrue(tracker.isCurrent(gen2))

        tracker.runIfCurrent(gen2) { status = "Connected-2" }
        assertEquals("Connected-2", status)

        // Late callbacks (message, close, failure) from old socket gen1 must be ignored
        val ranLateMessage = tracker.runIfCurrent(gen1) { status = "Corrupted-by-gen1" }
        assertFalse(ranLateMessage)
        assertEquals("Connected-2", status)

        val oldLostBackoff = tracker.onSocketLost(gen1)
        assertNull("Old socket close must not trigger reconnect or invalidate gen2", oldLostBackoff)
        assertTrue("gen2 must still be current after gen1 closes", tracker.isCurrent(gen2))
        assertEquals("Connected-2", status)
    }

    @Test
    fun exponentialBackoffProgressesAndResetsOnSuccessfulOpen() {
        val tracker = ConnectionTracker()

        val gen1 = tracker.nextGeneration()
        assertEquals(1_000L, tracker.onSocketLost(gen1))
        assertFalse("Lost socket generation is invalidated", tracker.isCurrent(gen1))

        val gen2 = tracker.nextGeneration()
        assertEquals(2_000L, tracker.onSocketLost(gen2))

        val gen3 = tracker.nextGeneration()
        assertEquals(4_000L, tracker.onSocketLost(gen3))

        val gen4 = tracker.nextGeneration()
        assertEquals(8_000L, tracker.onSocketLost(gen4))

        val gen5 = tracker.nextGeneration()
        assertEquals(16_000L, tracker.onSocketLost(gen5))

        val gen6 = tracker.nextGeneration()
        assertEquals(32_000L, tracker.onSocketLost(gen6))

        // Capped at 32s
        val gen7 = tracker.nextGeneration()
        assertEquals(32_000L, tracker.onSocketLost(gen7))

        // Successful open resets backoff
        val gen8 = tracker.nextGeneration()
        assertTrue(tracker.onSocketOpened(gen8))
        assertEquals(1_000L, tracker.onSocketLost(gen8))
    }

    @Test
    fun defaultHostIsLoopbackAndDetectsLoopbackVersusLanHosts() {
        assertEquals("127.0.0.1:8767", ConnectionTracker.DEFAULT_HOST)
        assertTrue(ConnectionTracker.isLoopbackHost("127.0.0.1:8767"))
        assertTrue(ConnectionTracker.isLoopbackHost("ws://127.0.0.1:8767/pocket"))
        assertTrue(ConnectionTracker.isLoopbackHost("localhost:8767"))
        assertTrue(ConnectionTracker.isLoopbackHost("[::1]:8767"))
        assertFalse(ConnectionTracker.isLoopbackHost("192.0.2.42:8767"))
        assertFalse(ConnectionTracker.isLoopbackHost("ws://192.0.2.15:8767/pocket"))
    }
}
