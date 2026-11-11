package com.hermes.pocket.wear

sealed class EnrollmentState {
    data object Disconnected : EnrollmentState()
    data object Connecting : EnrollmentState()

    data class Pending(
        val matchCode: String,
        val expiresAt: Long,
        val requestId: String = ""
    ) : EnrollmentState() {
        fun secondsRemaining(nowMillis: Long): Long {
            val remainingMs = expiresAt - nowMillis
            if (remainingMs <= 0L) return 0L
            return (remainingMs + 999L) / 1000L
        }
    }

    data object Ready : EnrollmentState()

    data class Failed(
        val code: String,
        val message: String = shortMessageForCode(code)
    ) : EnrollmentState()

    val canTalk: Boolean
        get() = this is Ready

    companion object {
        val PAIRING_FAILURE_CODES = setOf("denied", "pair_timeout", "busy")

        fun shortMessageForCode(code: String, serverMessage: String? = null): String {
            return when (code) {
                "denied" -> "Pairing denied"
                "pair_timeout" -> "Pairing timed out"
                "busy" -> "Too many pairing requests"
                else -> serverMessage?.takeIf { it.isNotBlank() } ?: "Error: $code"
            }
        }
    }
}

class EnrollmentMachine(
    initialToken: String? = null,
    private val onTokenSaved: (String) -> Unit = {},
    private val onTokenCleared: () -> Unit = {}
) {
    var state: EnrollmentState = EnrollmentState.Disconnected
        private set

    var token: String? = initialToken
        private set

    fun onConnecting() {
        state = EnrollmentState.Connecting
    }

    fun onDisconnected() {
        if (state !is EnrollmentState.Failed) {
            state = EnrollmentState.Disconnected
        }
    }

    fun onPairPending(
        matchCode: String,
        expiresInSeconds: Int,
        nowMillis: Long = System.currentTimeMillis(),
        requestId: String = ""
    ) {
        val expiresAt = nowMillis + expiresInSeconds.coerceAtLeast(0) * 1000L
        state = EnrollmentState.Pending(
            matchCode = matchCode,
            expiresAt = expiresAt,
            requestId = requestId
        )
    }

    fun onPaired(newToken: String) {
        token = newToken
        onTokenSaved(newToken)
    }

    fun onWelcome() {
        state = EnrollmentState.Ready
    }

    fun onError(code: String, message: String? = null) {
        when (code) {
            "revoked" -> {
                clearTokenAndReconnect()
            }
            in EnrollmentState.PAIRING_FAILURE_CODES -> {
                state = EnrollmentState.Failed(
                    code = code,
                    message = EnrollmentState.shortMessageForCode(code, message)
                )
            }
        }
    }

    fun onClosed(code: Int) {
        when (code) {
            4005 -> {
                clearTokenAndReconnect()
            }
            4003 -> {
                if (state !is EnrollmentState.Failed) {
                    state = EnrollmentState.Failed(
                        code = "pair_timeout",
                        message = EnrollmentState.shortMessageForCode("pair_timeout")
                    )
                }
            }
            else -> {
                if (state !is EnrollmentState.Failed) {
                    state = EnrollmentState.Connecting
                }
            }
        }
    }

    fun onRetry() {
        state = EnrollmentState.Connecting
    }

    fun updateToken(newToken: String?) {
        token = newToken
    }

    private fun clearTokenAndReconnect() {
        token = null
        onTokenCleared()
        state = EnrollmentState.Connecting
    }
}
