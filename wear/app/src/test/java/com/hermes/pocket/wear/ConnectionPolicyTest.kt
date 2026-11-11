package com.hermes.pocket.wear

import org.junit.Assert.*
import org.junit.Test

/**
 * The reconnect policy must never permanently give up: a hub restart, a sleeping laptop or a
 * Wi-Fi blip all have to recover on their own, without the owner force-stopping the app.
 */
class ConnectionPolicyTest {

    @Test
    fun socketLostKeepsReturningADelayLongAfterTheFastScheduleIsSpent() {
        val policy = ConnectionPolicy()
        policy.updateUsage(expectingResult = true, audioPlaying = false)
        assertTrue(policy.isInUse())

        // 40 losses is far past MAX_FOREGROUND_RETRIES (6); none of them may be a give-up.
        val losses = 40
        for (attempt in 1..losses) {
            val delay = policy.onSocketLost(policy.beginConnect("retry-$attempt"))
            assertNotNull(
                "Retry $attempt must still schedule a reconnect — the policy may never give up permanently",
                delay
            )
            assertTrue("Retry $attempt must schedule a positive delay", (delay ?: 0L) > 0L)
        }

        assertEquals(losses, policy.retryCount)
        assertTrue(policy.shouldBeConnected())
    }

    @Test
    fun reconnectDelayNeverExceedsTheSixtySecondCap() {
        val policy = ConnectionPolicy()
        policy.onActivityStarted()

        for (attempt in 1..120) {
            val delay = policy.onSocketLost(policy.beginConnect("retry"))
            assertNotNull("Retry $attempt must not give up", delay)
            assertTrue(
                "Retry $attempt scheduled ${delay}ms, above the ${ConnectionPolicy.SLOW_RETRY_MS}ms slow-retry cap",
                delay != null && delay in 1L..ConnectionPolicy.MAX_BACKOFF_MS
            )
        }

        // Everything past the fast schedule sits exactly on the cap, forever.
        repeat(5) {
            val delay = policy.onSocketLost(policy.beginConnect("retry"))!!
            assertEquals(ConnectionPolicy.SLOW_RETRY_MS, delay)
        }
    }

    @Test
    fun slowRetryStaysFalseThroughTheFastScheduleAndTurnsTrueAfterwards() {
        val policy = ConnectionPolicy()
        policy.updateUsage(expectingResult = true, audioPlaying = false)

        val fastSchedule = longArrayOf(2_000L, 4_000L, 8_000L, 16_000L, 32_000L, 60_000L)
        fastSchedule.forEachIndexed { index, expected ->
            val delay = policy.onSocketLost(policy.beginConnect("retry"))!!
            assertEquals("Retry ${index + 1} must follow the fast backoff schedule", expected, delay)
            assertEquals((index + 1).toLong(), policy.retryCount.toLong())
            assertFalse(
                "Retry ${index + 1} is still inside the fast schedule, so it must not be flagged as slow",
                policy.slowRetry
            )
        }

        // The first retry after the fast schedule is spent is the one that flips the flag.
        assertEquals(ConnectionPolicy.SLOW_RETRY_MS, policy.onSocketLost(policy.beginConnect("retry"))!!)
        assertTrue("Once the fast retries are spent the policy must report a slow retry", policy.slowRetry)

        repeat(10) {
            assertEquals(ConnectionPolicy.SLOW_RETRY_MS, policy.onSocketLost(policy.beginConnect("retry"))!!)
            assertTrue("Slow retry must stay lit while still trying slowly", policy.slowRetry)
        }
    }

    @Test
    fun socketOpenResetsRetryCountSlowRetryAndAnyGaveUpState() {
        val policy = ConnectionPolicy()
        policy.updateUsage(expectingResult = true, audioPlaying = false)

        repeat(12) { policy.onSocketLost(policy.beginConnect("retry")) }
        assertEquals(12, policy.retryCount)
        assertTrue(policy.slowRetry)
        assertFalse(policy.connected)

        val openGeneration = policy.beginConnect("socket came back")
        assertTrue(policy.onSocketOpened(openGeneration))

        assertTrue(policy.connected)
        assertEquals("A live socket must restart the retry count", 0, policy.retryCount)
        assertFalse("A live socket must clear the slow-retry flag", policy.slowRetry)
        assertFalse("A live socket must clear any gave-up state", policy.gaveUp)

        // Back on the fast schedule from the top, not stuck on the 60s tail.
        val nextDelay = policy.onSocketLost(policy.beginConnect("retry"))!!
        assertEquals(fastRetryDelay(0), nextDelay)
        assertFalse(policy.slowRetry)
        assertEquals(1, policy.retryCount)

        // The on-screen "tap to retry" path clears the same state.
        repeat(10) { policy.onSocketLost(policy.beginConnect("retry")) }
        assertTrue(policy.slowRetry)
        policy.resetForRetry()
        assertEquals(0, policy.retryCount)
        assertFalse(policy.slowRetry)
        assertFalse(policy.gaveUp)
        assertEquals(fastRetryDelay(0), policy.onSocketLost(policy.beginConnect("retry"))!!)
    }

    @Test
    fun idleBackgroundPolicyReturnsNullSoNothingSpinsInBackground() {
        val policy = ConnectionPolicy()

        assertFalse(policy.isInUse())
        assertFalse("Nothing is waiting on the link, so there is nothing to reconnect to", policy.shouldBeConnected())
        assertFalse(policy.shouldKeepServiceAlive())

        assertNull(
            "A policy nobody is using must not schedule a background retry",
            policy.onSocketLost(policy.beginConnect("background"))
        )
        assertEquals(0, policy.retryCount)
        assertFalse(policy.slowRetry)

        // Same story after a session: once the watch is backgrounded with no pending reply
        // and no audio playing, the retry loop must not keep spinning.
        policy.onActivityStarted()
        policy.updateUsage(expectingResult = true, audioPlaying = false)
        assertTrue(policy.isInUse())
        repeat(8) {
            assertNotNull("An in-use policy must keep retrying", policy.onSocketLost(policy.beginConnect("retry")))
        }
        val retriesWhileUsed = policy.retryCount

        policy.updateUsage(expectingResult = false, audioPlaying = false)
        policy.onActivityStopped(nowMs = 10_000L)
        assertFalse(policy.isInUse())

        assertNull(
            "Background, no pending reply, no audio — a lost socket must schedule nothing",
            policy.onSocketLost(policy.beginConnect("background"))
        )
        assertEquals("A background policy must not count retries it never scheduled", retriesWhileUsed, policy.retryCount)
    }

    private fun fastRetryDelay(attemptIndex: Int): Long = ConnectionPolicy.backoffDelayMs(attemptIndex)
}
