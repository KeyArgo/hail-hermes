package com.hermes.pocket.wear

class ConnectionTracker {
    @Volatile
    var currentGeneration: Int = 0
        private set

    @Volatile
    var attempt: Int = 0
        private set

    @Synchronized
    fun nextGeneration(): Int {
        currentGeneration += 1
        return currentGeneration
    }

    @Synchronized
    fun isCurrent(generation: Int): Boolean {
        return generation != 0 && generation == currentGeneration
    }

    @Synchronized
    fun runIfCurrent(generation: Int, block: () -> Unit): Boolean {
        if (!isCurrent(generation)) return false
        block()
        return true
    }

    @Synchronized
    fun onSocketOpened(generation: Int): Boolean {
        if (!isCurrent(generation)) return false
        attempt = 0
        return true
    }

    @Synchronized
    fun onSocketLost(generation: Int): Long? {
        if (!isCurrent(generation)) return null
        currentGeneration += 1
        val delayMs = (1000L shl attempt.coerceAtMost(MAX_BACKOFF_SHIFT))
        attempt += 1
        return delayMs
    }

    @Synchronized
    fun resetBackoff() {
        attempt = 0
    }

    companion object {
        const val DEFAULT_HOST = "127.0.0.1:8767"
        const val MAX_BACKOFF_SHIFT = 5

        fun extractHostName(hostInput: String): String {
            var cleaned = hostInput.trim()
            if (cleaned.startsWith("ws://")) {
                cleaned = cleaned.removePrefix("ws://")
            } else if (cleaned.startsWith("wss://")) {
                cleaned = cleaned.removePrefix("wss://")
            }
            val slashIdx = cleaned.indexOf('/')
            if (slashIdx >= 0) {
                cleaned = cleaned.substring(0, slashIdx)
            }
            if (cleaned.startsWith("[")) {
                val closeBracket = cleaned.indexOf(']')
                if (closeBracket > 0) {
                    return cleaned.substring(1, closeBracket).lowercase()
                }
            }
            val colonIdx = cleaned.indexOf(':')
            if (colonIdx >= 0) {
                return cleaned.substring(0, colonIdx).lowercase()
            }
            return cleaned.lowercase()
        }

        fun isLoopbackHost(hostInput: String): Boolean {
            val host = extractHostName(hostInput)
            return host == "localhost" ||
                host == "::1" ||
                host == "127.0.0.1" ||
                host.startsWith("127.")
        }

        fun isLanHost(hostInput: String): Boolean {
            val host = extractHostName(hostInput)
            if (host.isEmpty() || isLoopbackHost(hostInput)) return false
            if (host.endsWith(".local") || host.endsWith(".lan") || host.endsWith(".home")) return true
            if (host.startsWith("fe80:") ||
                ((host.startsWith("fc") || host.startsWith("fd")) && host.contains(":"))
            ) return true
            val parts = host.split('.')
            if (parts.size == 4) {
                val octets = parts.map { it.toIntOrNull() ?: return false }
                if (octets.any { it !in 0..255 }) return false
                val a = octets[0]
                val b = octets[1]
                return a == 10 ||
                    (a == 192 && b == 168) ||
                    (a == 172 && b in 16..31) ||
                    (a == 169 && b == 254)
            }
            return false
        }

        fun shouldRequestWifi(hostInput: String, inForeground: Boolean): Boolean =
            inForeground && isLanHost(hostInput)

        fun buildWebSocketUrl(hostInput: String): String {
            val trimmed = hostInput.trim().ifEmpty { DEFAULT_HOST }
            val address = if (trimmed.startsWith("ws://") || trimmed.startsWith("wss://")) {
                trimmed.trimEnd('/')
            } else {
                "ws://${trimmed.trimEnd('/')}"
            }
            return if (address.endsWith("/pocket")) address else "$address/pocket"
        }
    }
}

class ConnectionPolicy(
    private val tracker: ConnectionTracker = ConnectionTracker()
) {
    var inForeground: Boolean = false
        private set
    var expectingResult: Boolean = false
        private set
    var audioPlaying: Boolean = false
        private set
    var pairingPending: Boolean = false
        private set
    var connected: Boolean = false
        private set
    var gaveUp: Boolean = false
        private set
    /** True once the fast retries are spent: we are still trying, just slowly. */
    var slowRetry: Boolean = false
        private set
    var retryCount: Int = 0
        private set
    var stoppedAtMs: Long? = null
        private set
    var lastReconnectReason: String = "none"
        private set

    val currentGeneration: Int
        get() = tracker.currentGeneration

    @Synchronized
    fun isInUse(): Boolean = inForeground || expectingResult || audioPlaying || pairingPending

    @Synchronized
    fun shouldBeConnected(): Boolean = isInUse() && !gaveUp

    @Synchronized
    fun shouldKeepServiceAlive(): Boolean = connected || isInUse()

    @Synchronized
    fun shouldSendPing(): Boolean = connected && isInUse()

    @Synchronized
    fun shouldRequestWifi(hostInput: String): Boolean =
        ConnectionTracker.shouldRequestWifi(hostInput, inForeground)

    @Synchronized
    fun onActivityStarted(nowMs: Long = 0L): Boolean {
        val wasInForeground = inForeground
        inForeground = true
        stoppedAtMs = null
        if (!wasInForeground) {
            retryCount = 0
            gaveUp = false
        }
        return !connected
    }

    @Synchronized
    fun onActivityStopped(nowMs: Long): Long {
        inForeground = false
        stoppedAtMs = nowMs
        return IDLE_DISCONNECT_DELAY_MS
    }

    @Synchronized
    fun updateUsage(expectingResult: Boolean, audioPlaying: Boolean, pairingPending: Boolean = this.pairingPending) {
        this.expectingResult = expectingResult
        this.audioPlaying = audioPlaying
        this.pairingPending = pairingPending
    }

    @Synchronized
    fun shouldDisconnectForIdle(nowMs: Long): Boolean {
        if (inForeground || expectingResult || audioPlaying) return false
        val stoppedAt = stoppedAtMs ?: return true
        return nowMs - stoppedAt >= IDLE_DISCONNECT_DELAY_MS
    }

    @Synchronized
    fun beginConnect(reason: String): Int {
        lastReconnectReason = reason
        connected = false
        return tracker.nextGeneration()
    }

    @Synchronized
    fun onSocketOpened(generation: Int): Boolean {
        if (!tracker.onSocketOpened(generation)) return false
        connected = true
        retryCount = 0
        gaveUp = false
        slowRetry = false
        return true
    }

    @Synchronized
    fun onSocketLost(generation: Int): Long? {
        if (!tracker.isCurrent(generation)) return null
        tracker.nextGeneration()
        connected = false
        if (!isInUse()) return null
        // Never stop trying. A hub restart, a sleeping laptop or a Wi-Fi blip must all
        // recover without the owner force-stopping the app, which is the single most
        // damaging thing that can happen in a demo. After the fast schedule we keep
        // retrying once a minute and say so on screen.
        val delayMs = if (retryCount >= MAX_FOREGROUND_RETRIES) SLOW_RETRY_MS else backoffDelayMs(retryCount)
        retryCount += 1
        slowRetry = retryCount > MAX_FOREGROUND_RETRIES
        return delayMs
    }

    @Synchronized
    fun onDisconnected(reason: String = lastReconnectReason) {
        lastReconnectReason = reason
        tracker.nextGeneration()
        connected = false
    }

    @Synchronized
    fun resetForRetry(reason: String = "tap to retry") {
        retryCount = 0
        gaveUp = false
        slowRetry = false
        lastReconnectReason = reason
    }

    @Synchronized
    fun isCurrent(generation: Int): Boolean = tracker.isCurrent(generation)

    @Synchronized
    fun runIfCurrent(generation: Int, block: () -> Unit): Boolean =
        tracker.runIfCurrent(generation, block)

    companion object {
        const val IDLE_DISCONNECT_DELAY_MS = 30_000L
        const val PING_INTERVAL_MS = 25_000L
        const val MAX_FOREGROUND_RETRIES = 6
        const val MAX_BACKOFF_MS = 60_000L
        /** Slow-but-alive cadence: fast enough to recover inside a demo, slow enough to spare the battery. */
        const val SLOW_RETRY_MS = 15_000L
        const val GIVE_UP_MESSAGE = "Can't reach your computer — tap to retry"
        val BACKOFF_SCHEDULE_MS = longArrayOf(2_000L, 4_000L, 8_000L, 16_000L, 32_000L, 60_000L)

        fun backoffDelayMs(attemptIndex: Int): Long {
            if (attemptIndex <= 0) return BACKOFF_SCHEDULE_MS[0]
            if (attemptIndex < BACKOFF_SCHEDULE_MS.size) return BACKOFF_SCHEDULE_MS[attemptIndex]
            return MAX_BACKOFF_MS
        }
    }
}
