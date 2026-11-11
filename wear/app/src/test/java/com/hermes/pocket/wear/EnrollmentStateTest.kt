package com.hermes.pocket.wear

import org.junit.Assert.*
import org.junit.Test

class EnrollmentStateTest {
    @Test
    fun pendingCannotStartTalkingReadyCan() {
        val machine = EnrollmentMachine(initialToken = null)
        assertEquals(EnrollmentState.Disconnected, machine.state)
        assertFalse(machine.state.canTalk)

        machine.onConnecting()
        assertEquals(EnrollmentState.Connecting, machine.state)
        assertFalse(machine.state.canTalk)

        machine.onPairPending(matchCode = "4821", expiresInSeconds = 300, nowMillis = 1_000_000L)
        val pending = machine.state as EnrollmentState.Pending
        assertEquals("4821", pending.matchCode)
        assertEquals(1_300_000L, pending.expiresAt)
        assertEquals(300L, pending.secondsRemaining(1_000_000L))
        assertEquals(120L, pending.secondsRemaining(1_180_000L))
        assertFalse("Pending must not allow starting talk", machine.state.canTalk)

        machine.onPaired("tok_approved_123")
        assertEquals("tok_approved_123", machine.token)
        machine.onWelcome()
        assertEquals(EnrollmentState.Ready, machine.state)
        assertTrue("Ready must allow starting talk", machine.state.canTalk)
    }

    @Test
    fun pairingErrorsTransitionToFailedAndRetryReturnsToConnecting() {
        for (code in listOf("denied", "pair_timeout", "busy")) {
            val machine = EnrollmentMachine(initialToken = null)
            machine.onConnecting()
            machine.onPairPending(matchCode = "9912", expiresInSeconds = 60, nowMillis = 5000L)
            machine.onError(code, "Server $code")

            val failed = machine.state as EnrollmentState.Failed
            assertEquals(code, failed.code)
            assertTrue(failed.message.isNotBlank())
            assertFalse(machine.state.canTalk)

            // Subsequent close 4003 keeps the Failed state visible for the Retry chip
            machine.onClosed(4003)
            assertTrue(machine.state is EnrollmentState.Failed)

            machine.onRetry()
            assertEquals(EnrollmentState.Connecting, machine.state)
        }
    }

    @Test
    fun revokedErrorOrClose4005ClearsTokenAndReturnsToConnecting() {
        // Case 1: error {code: "revoked"}
        var clearedTokenCount = 0
        val machine1 = EnrollmentMachine(
            initialToken = "tok_valid_1",
            onTokenCleared = { clearedTokenCount++ }
        )
        machine1.onConnecting()
        machine1.onWelcome()
        assertEquals(EnrollmentState.Ready, machine1.state)
        assertEquals("tok_valid_1", machine1.token)

        machine1.onError("revoked", "Device token revoked")
        assertNull("Token must be cleared on error revoked", machine1.token)
        assertEquals(1, clearedTokenCount)
        assertEquals(EnrollmentState.Connecting, machine1.state)
        assertFalse(machine1.state.canTalk)

        // Case 2: close 4005
        val machine2 = EnrollmentMachine(
            initialToken = "tok_valid_2",
            onTokenCleared = { clearedTokenCount++ }
        )
        machine2.onConnecting()
        machine2.onWelcome()
        assertEquals(EnrollmentState.Ready, machine2.state)

        machine2.onClosed(4005)
        assertNull("Token must be cleared on close 4005", machine2.token)
        assertEquals(2, clearedTokenCount)
        assertEquals(EnrollmentState.Connecting, machine2.state)
        assertFalse(machine2.state.canTalk)
    }
}
