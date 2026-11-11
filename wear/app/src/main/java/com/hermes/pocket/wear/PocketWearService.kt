package com.hermes.pocket.wear

import android.app.*
import android.util.Log
import android.content.*
import android.content.pm.ServiceInfo
import android.media.AudioManager
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiManager
import java.net.DatagramSocket
import java.net.DatagramPacket
import java.net.SocketTimeoutException
import android.os.*
import androidx.core.app.NotificationCompat
import androidx.datastore.preferences.core.*
import androidx.datastore.preferences.preferencesDataStore
import androidx.wear.tiles.TileService
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import okhttp3.*
import okio.ByteString
import okio.ByteString.Companion.toByteString
import java.util.UUID
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

private val Context.wearStore by preferencesDataStore("pocket_wear")

object PocketWearState {
    /** ElapsedRealtime of the last audio frame that actually arrived, so the UI can
     *  tell "she is speaking" apart from "the stream died and audio_end never came". */
    val lastAudioFrameMs = MutableStateFlow(0L)
    /** The models this watch is allowed to switch to, as key ("provider::model") to label. */
    val watchModels = MutableStateFlow<List<Pair<String, String>>>(emptyList())
    /** Which of those is in use right now. */
    val watchModel = MutableStateFlow("")
    val discovery = MutableStateFlow<SearchState>(SearchState.Idle)
    val status = MutableStateFlow("Starting")
    val screen = MutableStateFlow("idle")
    val enrollment = MutableStateFlow<EnrollmentState>(EnrollmentState.Disconnected)
    val transcriptState = MutableStateFlow(TranscriptState())
    val transcript = MutableStateFlow<List<String>>(emptyList())
    val serverStateEvents = MutableSharedFlow<Codec.StateMessage>(extraBufferCapacity = 16)
    val replyAudioEvents = MutableSharedFlow<String>(extraBufferCapacity = 16)
    // True while speech is actually coming out of the speaker, so the UI can always
    // offer a Stop that works even if the server still thinks the screen is "idle".
    val speakingAudio = MutableStateFlow(false)
    val mode = MutableStateFlow(Codec.ModeMessage(mode = Codec.MODE_QUICK))
    val sessions = MutableStateFlow<List<Codec.SessionItem>>(emptyList())
    val slots = MutableStateFlow<List<Codec.SlotItem>>(emptyList())
    const val DEFAULT_LISTEN_ON_OPEN = false
    val listenOnOpen = MutableStateFlow(DEFAULT_LISTEN_ON_OPEN)
    val conversationMode = MutableStateFlow(false)
    val conversationTimeoutSec = MutableStateFlow(20)
    val hapticsEnabled = MutableStateFlow(true)
    val developerMode = MutableStateFlow(false)
    val lastTurnTiming = MutableStateFlow<String?>(null)
    val deviceId = MutableStateFlow("")
    val prefsLoaded = MutableStateFlow(false)
    val volumeOffHint = MutableStateFlow(false)
    val socketConnected = MutableStateFlow(false)
    val lastReconnectReason = MutableStateFlow("none")
    val connectionGaveUp = MutableStateFlow(false)
    val expectingReply = MutableStateFlow(false)
    val workingCount = MutableStateFlow(0)
    val visibleError = MutableStateFlow<String?>(null)
    // What the resumed session was about. Shown for a few seconds so it is readable as well
    // as spoken; cleared when the next turn starts.
    val recapNotice = MutableStateFlow<String?>(null)
    val responseEvents = MutableStateFlow(0L)
    var service: PocketWearService? = null

    private val activityInForeground = AtomicBoolean(false)

    fun isActivityInForeground(): Boolean = activityInForeground.get()

    fun setActivityInForeground(foreground: Boolean) {
        activityInForeground.set(foreground)
        if (foreground) {
            service?.onActivityStarted()
        } else {
            service?.onActivityStopped()
        }
    }

    fun setExpectingReply(expecting: Boolean) {
        expectingReply.value = expecting
        service?.onExpectingReplyChanged(expecting)
    }
}

class PocketWearService : Service() {
    companion object {
        private const val PLAYBACK_NOTIFICATION_ID = 2
        private const val TAG = "HailHermes"
        const val CANT_REACH = "Can't reach your computer — tap to retry"
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val baseClient = OkHttpClient.Builder()
        .pingInterval(ConnectionPolicy.PING_INTERVAL_MS, TimeUnit.MILLISECONDS)
        .build()
    private val connectionPolicy = ConnectionPolicy()
    private val playback = PlaybackController(
        sink = AndroidAudioSink(),
        scope = scope,
        onStreamDrained = {
            PocketWearState.speakingAudio.value = false
            onPlaybackStreamDrained()
            PocketWearState.replyAudioEvents.tryEmit("audio_end")
        },
        // Barge-in, a lost connection and re-pairing all interrupt playback; stop the talking mouth too.
        onInterrupted = { PocketWearState.speakingAudio.value = false }
    )

    private var searchJob: Job? = null
    private var savedFp: String? = null
    private var hasSavedHost = false
    private val searchMachine = SearchMachine()
    private var socket: WebSocket? = null
    private var reconnect: Job? = null
    private var idleDisconnectJob: Job? = null
    private var pingJob: Job? = null
    private var boundNetwork: Network? = null
    private var wifiCallback: ConnectivityManager.NetworkCallback? = null
    private var inForeground: Boolean = false
    private var audioPlaying: Boolean = false
    private var replyPending: Boolean = false
    private var playbackForegroundActive: Boolean = false
    private var playbackNotification: android.app.Notification? = null

    private val hostKey = stringPreferencesKey("host")
    private val computerNameKey = stringPreferencesKey("computer_name")
    private val computerFpKey = stringPreferencesKey("computer_fp")
    private val idKey = stringPreferencesKey("id")
    private val tokenKey = stringPreferencesKey("token")
    private val listenOnOpenKey = booleanPreferencesKey("listen_on_open")
    private val conversationModeKey = booleanPreferencesKey("conversation_mode")
    private val conversationTimeoutSecKey = intPreferencesKey("conversation_timeout_sec")
    private val hapticsEnabledKey = booleanPreferencesKey("haptics_enabled")
    private val developerModeKey = booleanPreferencesKey("developer_mode")

    private var host = ConnectionTracker.DEFAULT_HOST
    private var id = ""
    private var token: String? = null

    private val enrollmentMachine = EnrollmentMachine(
        initialToken = null,
        onTokenSaved = { saved ->
            token = saved
            scope.launch {
                wearStore.edit { it[tokenKey] = saved }
            }
        },
        onTokenCleared = {
            token = null
            scope.launch {
                wearStore.edit { it.remove(tokenKey) }
            }
        }
    )

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        PocketWearState.service = this

        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel("wear_connection", "Watch Connection", NotificationManager.IMPORTANCE_LOW)
        )
        manager.createNotificationChannel(
            NotificationChannel("wear_messages", "Hermes Alerts", NotificationManager.IMPORTANCE_HIGH)
        )
        manager.createNotificationChannel(
            NotificationChannel("wear_playback", "Reply Playback", NotificationManager.IMPORTANCE_LOW)
        )
        playbackNotification = NotificationCompat.Builder(this, "wear_playback")
            .setSmallIcon(android.R.drawable.ic_lock_silent_mode_off)
            .setContentTitle("Hermes")
            .setContentText("Playing reply")
            .setContentIntent(openIntent())
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()

        scope.launch {
            wearStore.edit {
                if (it[idKey] == null) {
                    it[idKey] = UUID.randomUUID().toString()
                }
            }
            val prefs = wearStore.data.first()
            host = prefs[hostKey] ?: ConnectionTracker.DEFAULT_HOST
            hasSavedHost = prefs[hostKey] != null
            savedFp = prefs[computerFpKey]
            id = prefs[idKey]!!
            PocketWearState.deviceId.value = id
            token = prefs[tokenKey]
            PocketWearState.listenOnOpen.value =
                prefs[listenOnOpenKey] ?: PocketWearState.DEFAULT_LISTEN_ON_OPEN
            PocketWearState.conversationMode.value = prefs[conversationModeKey] ?: false
            PocketWearState.conversationTimeoutSec.value =
                (prefs[conversationTimeoutSecKey] ?: 20).let { if (it in setOf(10, 20, 30)) it else 20 }
            PocketWearState.hapticsEnabled.value = prefs[hapticsEnabledKey] ?: true
            PocketWearState.developerMode.value = prefs[developerModeKey] ?: false
            PocketWearState.prefsLoaded.value = true
            enrollmentMachine.updateToken(token)
            if (PocketWearState.isActivityInForeground()) {
                onActivityStarted()
            } else if (isReplyOrAudioActive()) {
                connectionPolicy.updateUsage(expectingResult = true, audioPlaying = audioPlaying)
                connect("pending reply")
            } else {
                updateWifiRequestForForegroundState()
                stopIfIdleAndDisconnected()
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_NOT_STICKY

    private fun openIntent(): PendingIntent = PendingIntent.getActivity(
        this,
        0,
        Intent(this, MainActivity::class.java),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
    )

    private fun requestTileUpdate() {
        try {
            TileService.getUpdater(this).requestUpdate(PocketTileService::class.java)
        } catch (_: Exception) {
        }
    }

    @Synchronized
    private fun isReplyExpected(): Boolean {
        val curScreen = PocketWearState.screen.value
        return replyPending ||
            PocketWearState.workingCount.value > 0 ||
            PocketWearState.expectingReply.value ||
            curScreen == "thinking" ||
            curScreen == "speaking"
    }

    @Synchronized
    private fun isReplyOrAudioActive(): Boolean = audioPlaying || isReplyExpected()

    @Synchronized
    private fun updatePingJob() {
        connectionPolicy.updateUsage(expectingResult = isReplyExpected(), audioPlaying = audioPlaying)
        if (socket != null && connectionPolicy.shouldSendPing()) {
            if (pingJob?.isActive == true) return
            pingJob = scope.launch {
                while (isActive) {
                    delay(ConnectionPolicy.PING_INTERVAL_MS)
                    val ws = synchronized(this@PocketWearService) {
                        if (connectionPolicy.shouldSendPing()) socket else null
                    } ?: break
                    ws.send(Codec.ping(System.currentTimeMillis()))
                }
            }
        } else {
            stopPingJob()
        }
    }

    @Synchronized
    private fun stopPingJob() {
        pingJob?.cancel()
        pingJob = null
    }

    /**
     * Promotes the service to a mediaPlayback foreground service while a reply is playing, so the
     * system keeps the audio alive with the screen off. Must never break playback: any failure is
     * logged (tag HailHermes) and the audio continues as before.
     */
    @Synchronized
    private fun promoteToPlaybackForeground() {
        if (playbackForegroundActive) return
        val notification = playbackNotification ?: return
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(
                    PLAYBACK_NOTIFICATION_ID,
                    notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
                )
            } else {
                @Suppress("DEPRECATION")
                startForeground(PLAYBACK_NOTIFICATION_ID, notification)
            }
            playbackForegroundActive = true
        } catch (e: Exception) {
            Log.w(TAG, "startForeground failed; audio continues without foreground promotion", e)
        }
    }

    @Synchronized
    private fun demoteFromPlaybackForeground() {
        if (!playbackForegroundActive) return
        playbackForegroundActive = false
        try {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } catch (e: Exception) {
            Log.w(TAG, "stopForeground failed", e)
        }
    }

    @Synchronized
    private fun onPlaybackStreamDrained() {
        audioPlaying = false
        if (PocketWearState.screen.value == "idle") replyPending = false
        onUsageStateChanged()
    }

    @Synchronized
    fun onExpectingReplyChanged(expecting: Boolean) {
        if (expecting && PocketWearState.prefsLoaded.value && socket == null && reconnect == null) {
            connectionPolicy.updateUsage(expectingResult = true, audioPlaying = audioPlaying)
            connect("conversation reply")
            return
        }
        onUsageStateChanged()
    }

    @Synchronized
    private fun onUsageStateChanged() {
        connectionPolicy.updateUsage(
            expectingResult = isReplyExpected(),
            audioPlaying = audioPlaying,
            // Keep the connection and the screen alive while a pairing code is on screen:
            // the owner has to walk to the computer to approve it.
            pairingPending = enrollmentMachine.state is EnrollmentState.Pending
        )
        updatePingJob()
        if (!inForeground && !isReplyOrAudioActive()) {
            val now = System.currentTimeMillis()
            if (connectionPolicy.shouldDisconnectForIdle(now)) {
                disconnectForIdle()
            } else if (idleDisconnectJob?.isActive != true) {
                val stoppedAt = connectionPolicy.stoppedAtMs ?: now
                val remaining = (ConnectionPolicy.IDLE_DISCONNECT_DELAY_MS - (now - stoppedAt))
                    .coerceAtLeast(0L)
                scheduleIdleDisconnect(remaining)
            }
        }
    }

    @Synchronized
    private fun scheduleIdleDisconnect(delayMs: Long = ConnectionPolicy.IDLE_DISCONNECT_DELAY_MS) {
        idleDisconnectJob?.cancel()
        idleDisconnectJob = scope.launch {
            delay(delayMs)
            synchronized(this@PocketWearService) {
                connectionPolicy.updateUsage(expectingResult = isReplyExpected(), audioPlaying = audioPlaying)
                if (connectionPolicy.shouldDisconnectForIdle(System.currentTimeMillis())) {
                    disconnectForIdle()
                }
            }
        }
    }

    @Synchronized
    private fun disconnectForIdle() {
        idleDisconnectJob?.cancel()
        idleDisconnectJob = null
        reconnect?.cancel()
        reconnect = null
        stopPingJob()
        releaseWifiNetwork()
        connectionPolicy.onDisconnected("idle timeout")
        PocketWearState.lastReconnectReason.value = "idle timeout"
        socket?.close(1000, "idle")
        socket = null
        PocketWearState.socketConnected.value = false
        PocketWearState.status.value = "Disconnected"
        stopIfIdleAndDisconnected()
    }

    @Synchronized
    private fun stopIfIdleAndDisconnected() {
        if (!inForeground && !isReplyOrAudioActive() && socket == null && reconnect == null) {
            stopSelf()
        }
    }

    @Synchronized
    fun onActivityStarted() {
        inForeground = true
        idleDisconnectJob?.cancel()
        idleDisconnectJob = null
        val shouldConnect = connectionPolicy.onActivityStarted(System.currentTimeMillis())
        PocketWearState.connectionGaveUp.value = false
        updateWifiRequestForForegroundState()
        updatePingJob()
        if (PocketWearState.prefsLoaded.value && socket == null && reconnect == null && shouldConnect) {
            if (hasSavedHost) connect("activity started") else {
                startSearch()
                connect("adb fallback")
            }
        }
    }

    @Synchronized
    fun onActivityStopped() {
        inForeground = false
        stopSearch()
        // A reply or its audio can outlive the visible screen. Releasing the Wi-Fi network or
        // cancelling the reconnect under it kills the socket mid-turn, which is what the
        // screen-off tests were hitting.
        val turnActive = isReplyOrAudioActive()
        if (!turnActive) {
            reconnect?.cancel()
            reconnect = null
        }
        val delayMs = connectionPolicy.onActivityStopped(System.currentTimeMillis())
        if (!turnActive) releaseWifiNetwork()
        updatePingJob()
        if (socket == null && !isReplyOrAudioActive()) {
            stopIfIdleAndDisconnected()
        } else {
            scheduleIdleDisconnect(delayMs)
        }
    }

    @Synchronized
    private fun updateWifiRequestForForegroundState() {
        if (!connectionPolicy.shouldRequestWifi(host) && !(inForeground && PocketWearState.discovery.value !is SearchState.Idle && PocketWearState.discovery.value !is SearchState.NotFound)) {
            releaseWifiNetwork()
            return
        }
        if (wifiCallback != null) return
        val cm = getSystemService(ConnectivityManager::class.java) ?: return
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .build()
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                synchronized(this@PocketWearService) {
                    if (wifiCallback !== this || !inForeground) return
                    val isDifferent = boundNetwork != network
                    boundNetwork = network
                    if (isDifferent && socket == null && searchJob == null && hasSavedHost) {
                        reconnectNow("wifi available")
                    }
                }
            }

            override fun onLost(network: Network) {
                synchronized(this@PocketWearService) {
                    if (boundNetwork == network) {
                        boundNetwork = null
                    }
                }
            }
        }
        try {
            wifiCallback = callback
            cm.requestNetwork(request, callback)
        } catch (_: Exception) {
            wifiCallback = null
        }
    }

    @Synchronized
    private fun releaseWifiNetwork() {
        val cb = wifiCallback
        wifiCallback = null
        boundNetwork = null
        if (cb != null) {
            try {
                getSystemService(ConnectivityManager::class.java)?.unregisterNetworkCallback(cb)
            } catch (_: Exception) {
            }
        }
    }

    private fun activeHttpClient(): OkHttpClient {
        val net = synchronized(this) { boundNetwork }
        return if (net != null && connectionPolicy.shouldRequestWifi(host)) {
            baseClient.newBuilder()
                .socketFactory(net.socketFactory)
                .build()
        } else {
            baseClient
        }
    }

    @Synchronized
    private fun connect(reason: String = "connect") {
        Log.i(TAG, "connect: $reason")
        if (!inForeground && !isReplyOrAudioActive()) {
            Log.i(TAG, "connect skipped: app not in foreground")
            stopIfIdleAndDisconnected()
            return
        }
        reconnect?.cancel()
        reconnect = null
        stopPingJob()
        socket?.close(1000, "reconnecting")
        socket = null
        PocketWearState.socketConnected.value = false
        PocketWearState.connectionGaveUp.value = false
        PocketWearState.lastReconnectReason.value = reason

        val current = connectionPolicy.beginConnect(reason)
        enrollmentMachine.onConnecting()
        PocketWearState.enrollment.value = enrollmentMachine.state
        PocketWearState.status.value = "Connecting"
        requestTileUpdate()
        val url = ConnectionTracker.buildWebSocketUrl(host)

        try {
            socket = activeHttpClient().newWebSocket(
                Request.Builder().url(url).build(),
                object : WebSocketListener() {
                    override fun onOpen(ws: WebSocket, response: Response) {
                        if (!connectionPolicy.onSocketOpened(current)) {
                            ws.close(1000, "replaced")
                            return
                        }
                        stopSearch()
                        PocketWearState.socketConnected.value = true
                        PocketWearState.connectionGaveUp.value = false
                        Log.i(TAG, "socket open")
                        if (PocketWearState.visibleError.value?.let { it.startsWith("Lost connection") || it.startsWith("Can't reach") } == true) {
                            PocketWearState.visibleError.value = null
                        }
                        PocketWearState.status.value = "Connected"
                        updatePingJob()
                        ws.send(Codec.hello(id = id, token = token, haptic = hasVibrator()))
                        requestTileUpdate()
                    }

                    override fun onMessage(ws: WebSocket, text: String) {
                        // Never run handle() while holding the policy/tracker locks: handle()
                        // takes the service lock, and several service-side paths take the
                        // policy lock after it, which deadlocks both threads and leaves the
                        // UI frozen on the last state (the "stuck on Speaking..." report).
                        if (!connectionPolicy.isCurrent(current)) return
                        handle(text)
                    }

                    override fun onMessage(ws: WebSocket, bytes: ByteString) {
                        if (!connectionPolicy.isCurrent(current)) return
                        PocketWearState.lastAudioFrameMs.value = android.os.SystemClock.elapsedRealtime()
                        playback.onAudioFrame(bytes.toByteArray())
                    }

                    override fun onClosing(ws: WebSocket, code: Int, reason: String) {
                        ws.close(code, reason)
                    }

                    override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                        lost(current, code)
                    }

                    override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                        lost(current, null)
                    }
                }
            )
        } catch (_: IllegalArgumentException) {
            PocketWearState.status.value = "Invalid hub URL"
            lost(current, null)
        }
    }

    @Synchronized
    private fun lost(current: Int, closeCode: Int?) {
        if (!connectionPolicy.isCurrent(current)) return
        Log.i(TAG, "socket lost (code=$closeCode, turn active=${isReplyOrAudioActive()})")
        val lostDuringTurn = isReplyOrAudioActive()
        if (lostDuringTurn) {
            // Not an error the user has to fix: the app retries by itself. Red text telling
            // someone to tap makes a self-healing reconnect look like a failure — and it is
            // the first thing anyone sees in a demo.
            PocketWearState.visibleError.value = null
            PocketWearState.status.value = "Reconnecting…"
            buzz("short")
        }
        stopPingJob()
        socket?.close(1000, "lost")
        socket = null
        audioPlaying = false
        replyPending = lostDuringTurn
        PocketWearState.socketConnected.value = false
        PocketWearState.transcriptState.update { it.startNewTurn() }
        playback.interrupt()
        demoteFromPlaybackForeground()
        PocketWearState.screen.value = "idle"
        PocketWearState.serverStateEvents.tryEmit(Codec.StateMessage(screen = "idle"))
        if (closeCode != null) {
            enrollmentMachine.onClosed(closeCode)
        } else {
            enrollmentMachine.onDisconnected()
        }
        PocketWearState.enrollment.value = enrollmentMachine.state

        val currentEnrollment = enrollmentMachine.state
        if (currentEnrollment is EnrollmentState.Failed) {
            connectionPolicy.onDisconnected("enrollment failed")
            PocketWearState.lastReconnectReason.value = "enrollment failed"
            PocketWearState.status.value = currentEnrollment.message
            requestTileUpdate()
            reconnect?.cancel()
            reconnect = null
            stopIfIdleAndDisconnected()
            return
        }

        if (inForeground && !lostDuringTurn) {
            // Discovery only helps a first connection. A visible app with a saved host must
            // still reconnect on the normal backoff, otherwise a hub that stops announcing
            // itself (LAN switch off, adb port forward) leaves the watch offline for good.
            if (searchJob == null && PocketWearState.discovery.value !is SearchState.NotFound) startSearch()
        }
        connectionPolicy.updateUsage(expectingResult = isReplyExpected(), audioPlaying = audioPlaying)
        val delayMs = connectionPolicy.onSocketLost(current)
        if (delayMs == null) {
            reconnect?.cancel()
            reconnect = null
            if (connectionPolicy.slowRetry) {
                PocketWearState.connectionGaveUp.value = true
                PocketWearState.lastReconnectReason.value = "slow retry (60s)"
                PocketWearState.status.value = "Trying to reach your computer…"
            } else {
                PocketWearState.lastReconnectReason.value = "background disconnect"
                PocketWearState.status.value = "Disconnected"
                stopIfIdleAndDisconnected()
            }
            requestTileUpdate()
            return
        }

        val attemptNum = connectionPolicy.retryCount
        val reason = if (closeCode != null) "closed ($closeCode) #$attemptNum" else "socket lost #$attemptNum"
        PocketWearState.lastReconnectReason.value = reason
        PocketWearState.status.value = "Reconnecting"
        requestTileUpdate()

        reconnect?.cancel()
        reconnect = scope.launch {
            // While we are on the slow cadence, also listen for the hub's discovery
            // beacon: it announces itself every few seconds, so a restarted hub is
            // noticed in ~2s instead of waiting out the whole backoff. Without this the
            // watch sat offline for over a minute after a hub restart, which is far too
            // slow to show on stage.
            if (connectionPolicy.slowRetry && inForeground) {
                startSearch()
            }
            delay(delayMs)
            synchronized(this@PocketWearService) {
                if (inForeground || isReplyOrAudioActive()) {
                    connect(reason)
                } else {
                    stopIfIdleAndDisconnected()
                }
            }
        }
    }

    private fun handle(raw: String) {
        val msg = Codec.decode(raw) ?: return
        when (Codec.field(msg, "type")) {
            "models" -> {
                PocketWearState.watchModels.value = Codec.parseModelItems(msg)
            }
            "settings", "settings_changed" -> {
                val provider = Codec.field(msg, "provider").orEmpty()
                val model = Codec.field(msg, "model").orEmpty()
                PocketWearState.watchModel.value = "$provider::$model"
                val presets = msg["presets"] as? kotlinx.serialization.json.JsonArray
                if (presets != null) {
                    PocketWearState.watchModels.value = presets.mapNotNull { el ->
                        val item = el as? kotlinx.serialization.json.JsonObject ?: return@mapNotNull null
                        val src = Codec.field(item, "source") ?: return@mapNotNull null
                        val id = Codec.field(item, "id").orEmpty()
                        val label = Codec.field(item, "label") ?: "$src $id"
                        ("$src::$id") to label
                    }
                }
            }
            "recap" -> {
                val line = Codec.field(msg, "line").orEmpty()
                if (line.isNotEmpty()) {
                    PocketWearState.transcript.value = PocketWearState.transcript.value + "Resume: $line"
                    PocketWearState.recapNotice.value = line
                    scope.launch {
                        delay(12_000)
                        if (PocketWearState.recapNotice.value == line) {
                            PocketWearState.recapNotice.value = null
                        }
                    }
                }
            }
            "setting_error" -> {
                PocketWearState.visibleError.value = when (Codec.field(msg, "code")) {
                    "not_allowed" -> "Change that on your computer"
                    "bad_value" -> "That choice isn't available"
                    "bad_character" -> "That character isn't available"
                    "model_unreachable" -> "Couldn't reach that model"
                    else -> "That setting can't be changed"
                }
            }
            "welcome" -> {
                enrollmentMachine.onWelcome()
                PocketWearState.enrollment.value = enrollmentMachine.state
                PocketWearState.status.value = "Ready"
                // A fresh connection has no turn in flight. Without this reset a socket
                // lost mid-turn leaves replyPending/workingCount set forever, so the app
                // never reports idle again and nothing clears the stale "Speaking..." UI.
                synchronized(this) {
                    replyPending = false
                    audioPlaying = false
                }
                PocketWearState.workingCount.value = 0
                val selectedMode = PocketWearState.mode.value
                if (selectedMode.mode != Codec.MODE_QUICK) {
                    socket?.send(Codec.setMode(selectedMode.mode, selectedMode.sessionId))
                }
                val normScreen = Codec.normalizeScreen(Codec.field(msg, "state"))
                PocketWearState.screen.value = normScreen
                PocketWearState.serverStateEvents.tryEmit(Codec.StateMessage(screen = normScreen))
                requestTileUpdate()
            }
            "pair_pending" -> {
                val pending = Codec.parsePairPending(msg) ?: return
                enrollmentMachine.onPairPending(
                    matchCode = pending.matchCode,
                    expiresInSeconds = pending.expiresIn,
                    nowMillis = System.currentTimeMillis(),
                    requestId = pending.requestId
                )
                PocketWearState.enrollment.value = enrollmentMachine.state
                PocketWearState.status.value = "Pair Pending"
                requestTileUpdate()
            }
            "paired" -> {
                val paired = Codec.parsePaired(msg) ?: return
                enrollmentMachine.onPaired(paired.token)
                PocketWearState.enrollment.value = enrollmentMachine.state
            }
            "working" -> {
                val working = Codec.parseWorking(msg) ?: return
                PocketWearState.workingCount.value = working.count
                onUsageStateChanged()
            }
            "state" -> {
                val parsedState = Codec.parseState(msg) ?: return
                val state = parsedState.screen
                if (parsedState.timingText != null) {
                    PocketWearState.lastTurnTiming.value = parsedState.timingText
                }
                synchronized(this) {
                    if (state == "thinking" || state == "speaking") {
                        replyPending = true
                        // A new turn started, so any error from the last one is stale.
                        PocketWearState.visibleError.value = null
                    } else if (state == "idle" && !audioPlaying) {
                        replyPending = false
                    }
                }
                PocketWearState.screen.value = state
                PocketWearState.serverStateEvents.tryEmit(parsedState)
                if (state == "speaking") PocketWearState.volumeOffHint.value = isMediaVolumeZero()
                playback.onStateChanged(state)
                if (state != "speaking") demoteFromPlaybackForeground()
                onUsageStateChanged()
                requestTileUpdate()
            }
            "audio_start" -> {
                PocketWearState.responseEvents.update { it + 1 }
                PocketWearState.lastAudioFrameMs.value = android.os.SystemClock.elapsedRealtime()
                PocketWearState.speakingAudio.value = true
                buzz("double")
                synchronized(this) {
                    audioPlaying = true
                    replyPending = true
                }
                PocketWearState.volumeOffHint.value = isMediaVolumeZero()
                promoteToPlaybackForeground()
                playback.onAudioStart()
                PocketWearState.replyAudioEvents.tryEmit("audio_start")
                onUsageStateChanged()
            }
            "audio_end" -> {
                playback.onAudioEnd()
            }
            "mode" -> {
                val parsed = Codec.parseMode(msg) ?: return
                val fallbackTitle = parsed.title
                    ?: PocketWearState.slots.value.firstOrNull { it.id == parsed.sessionId }?.title
                    ?: PocketWearState.sessions.value.firstOrNull { it.id == parsed.sessionId }?.title
                    ?: PocketWearState.mode.value.takeIf { it.sessionId == parsed.sessionId }?.title
                PocketWearState.mode.value = parsed.copy(title = fallbackTitle)
            }
            "sessions" -> {
                val parsed = Codec.parseSessions(msg) ?: return
                PocketWearState.sessions.value = parsed.items
                val cur = PocketWearState.mode.value
                if (cur.mode == Codec.MODE_SESSION && cur.title.isNullOrBlank()) {
                    parsed.items.firstOrNull { it.id == cur.sessionId }?.title?.let {
                        PocketWearState.mode.value = cur.copy(title = it)
                    }
                }
            }
            "slots" -> {
                val parsed = Codec.parseSlots(msg) ?: return
                PocketWearState.slots.value = parsed.items
                val cur = PocketWearState.mode.value
                if (cur.mode == Codec.MODE_SESSION && cur.title.isNullOrBlank()) {
                    parsed.items.firstOrNull { it.id == cur.sessionId }?.title?.let {
                        PocketWearState.mode.value = cur.copy(title = it)
                    }
                }
            }
            "transcript" -> {
                val role = Codec.field(msg, "role") ?: "assistant"
                val text = Codec.field(msg, "text") ?: return
                PocketWearState.responseEvents.update { it + 1 }
                val isFinal = Codec.boolField(msg, "final") ?: true
                Codec.extractTimingSummary(msg)?.let { PocketWearState.lastTurnTiming.value = it }
                val next = PocketWearState.transcriptState.updateAndGet { current ->
                    current.onTranscript(role = role, text = text, isFinal = isFinal)
                }
                PocketWearState.transcript.value = next.formattedLines
                if (isFinal) {
                    requestTileUpdate()
                }
            }
            "notify" -> {
                val title = Codec.field(msg, "title") ?: "Hermes"
                val body = Codec.field(msg, "body") ?: ""
                buzz("notify")
                postNotification(title, body)
            }
            "haptic" -> {
                val haptic = Codec.parseHaptic(msg) ?: return
                buzz(haptic.pattern)
            }
            "tool_event" -> {
                val name = Codec.field(msg, "name") ?: ""
                val args = Codec.objectField(msg, "args")
                val pattern = args?.let { Codec.field(it, "pattern") } ?: "short"
                buzz(if (name == "device_haptic") pattern else "short")
            }
            "error" -> {
                val err = Codec.parseError(msg) ?: return
                synchronized(this) { replyPending = false }
                PocketWearState.screen.value = "idle"
                PocketWearState.serverStateEvents.tryEmit(Codec.StateMessage(screen = "idle"))
                onUsageStateChanged()
                enrollmentMachine.onError(err.code, err.message)
                PocketWearState.enrollment.value = enrollmentMachine.state
                val userError = when (err.code) {
                    "stt_failed" -> "Could not hear you · tap to retry"
                    "agent_failed" -> "Hermes failed · tap to retry"
                    "tts_failed" -> "Speech failed · tap to retry"
                    "too_long" -> "Too long · tap to retry"
                    "too_short" -> "Hold to talk"
                    else -> null
                }
                if (userError != null) {
                    PocketWearState.visibleError.value = userError
                    buzz("short")
                    synchronized(this) { replyPending = false }
                    PocketWearState.screen.value = "idle"
                    PocketWearState.serverStateEvents.tryEmit(Codec.StateMessage(screen = "idle"))
                    onUsageStateChanged()
                }
                when {
                    err.code == "revoked" -> {
                        PocketWearState.serverStateEvents.tryEmit(Codec.StateMessage(screen = "idle"))
                        PocketWearState.status.value = "Reconnecting"
                        requestTileUpdate()
                        reconnectNow("revoked")
                    }
                    err.code == "too_long" -> {
                        PocketWearState.serverStateEvents.tryEmit(Codec.StateMessage(screen = "idle"))
                        val displayMsg = err.message.ifEmpty { err.code }
                        PocketWearState.status.value = "Err: $displayMsg"
                        requestTileUpdate()
                    }
                    err.code == "bad_session" -> {
                        PocketWearState.mode.value = Codec.ModeMessage(mode = Codec.MODE_QUICK)
                        val displayMsg = err.message.ifEmpty { err.code }
                        PocketWearState.status.value = "Err: $displayMsg"
                        requestTileUpdate()
                    }
                    enrollmentMachine.state is EnrollmentState.Failed -> {
                        val failed = enrollmentMachine.state as EnrollmentState.Failed
                        PocketWearState.status.value = failed.message
                        requestTileUpdate()
                    }
                    else -> {
                        val displayMsg = err.message.ifEmpty { err.code }
                        PocketWearState.status.value = "Err: $displayMsg"
                        requestTileUpdate()
                    }
                }
            }
        }
    }

    private fun postNotification(title: String, body: String) {
        val notification = NotificationCompat.Builder(this, "wear_messages")
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setContentIntent(openIntent())
            .setAutoCancel(true)
            .build()
        try {
            getSystemService(NotificationManager::class.java)
                .notify((System.currentTimeMillis() and 0x7fffffff).toInt(), notification)
        } catch (_: SecurityException) {
        }
    }

    private fun resolveVibrator(): Vibrator? = if (Build.VERSION.SDK_INT >= 31) {
        getSystemService(VibratorManager::class.java)?.defaultVibrator
    } else {
        @Suppress("DEPRECATION")
        getSystemService(Vibrator::class.java)
    }

    fun hasVibrator(): Boolean = try {
        resolveVibrator()?.hasVibrator() == true
    } catch (_: Exception) {
        false
    }

    fun buzz(pattern: String = "short") {
        if (!PocketWearState.hapticsEnabled.value) return
        val vibrator = resolveVibrator() ?: return
        if (!vibrator.hasVibrator()) return
        val effect = when (pattern.lowercase()) {
            "tick", "soft", "thinking" -> try {
                VibrationEffect.createPredefined(VibrationEffect.EFFECT_TICK)
            } catch (_: Exception) {
                VibrationEffect.createOneShot(35, VibrationEffect.DEFAULT_AMPLITUDE)
            }
            "double", "notify" -> VibrationEffect.createWaveform(longArrayOf(0, 70, 60, 100), -1)
            "long", "urgent" -> VibrationEffect.createOneShot(300, VibrationEffect.DEFAULT_AMPLITUDE)
            else -> VibrationEffect.createOneShot(80, VibrationEffect.DEFAULT_AMPLITUDE)
        }
        try {
            vibrator.vibrate(effect)
        } catch (_: Exception) {
        }
    }

    fun isMediaVolumeZero(): Boolean = try {
        val am = getSystemService(AudioManager::class.java)
        am != null && am.getStreamVolume(AudioManager.STREAM_MUSIC) <= 0
    } catch (_: Exception) {
        false
    }

    fun raiseMediaVolumeToHalf() {
        try {
            val am = getSystemService(AudioManager::class.java) ?: return
            val max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
            if (max > 0) {
                am.setStreamVolume(AudioManager.STREAM_MUSIC, maxOf(1, (max + 1) / 2), AudioManager.FLAG_SHOW_UI)
            }
        } catch (_: Exception) {
        } finally {
            PocketWearState.volumeOffHint.value = false
        }
    }

    /** Opening the mic leaves any current reply playing; the hub queues the turn. */
    fun onUserListenStart() {
        if (!enrollmentMachine.state.canTalk) return
        PocketWearState.volumeOffHint.value = false
        onUsageStateChanged()
    }

    fun sendText(text: String) {
        if (!enrollmentMachine.state.canTalk) return
        PocketWearState.transcriptState.update { it.startNewTurn() }
        if (text.isNotBlank()) {
            if (socket?.send(Codec.text(text)) == true) {
                synchronized(this) { replyPending = true }
                onUsageStateChanged()
                buzz("tick")
            } else {
                PocketWearState.status.value = "Offline"
            }
        }
    }

    fun sendAudioStart(): Boolean {
        if (!enrollmentMachine.state.canTalk) return false
        PocketWearState.volumeOffHint.value = false
        PocketWearState.transcriptState.update { it.startNewTurn() }
        return socket?.send(Codec.audioStart()) == true
    }

    fun sendAudioFrame(pcm: ByteArray): Boolean {
        if (!enrollmentMachine.state.canTalk) return false
        val evenLen = pcm.size and 1.inv()
        if (evenLen <= 0) return false
        return socket?.send(pcm.toByteString(0, evenLen)) == true
    }

    fun sendAudioEnd(): Boolean {
        if (!enrollmentMachine.state.canTalk) return false
        val sent = socket?.send(Codec.audioEnd()) == true
        Log.i(TAG, "audio_end sent=$sent")
        if (sent) {
            synchronized(this) { replyPending = true }
            onUsageStateChanged()
            PocketWearState.visibleError.value = null
        }
        return sent
    }

    fun requestSessions(limit: Int? = null): Boolean {
        if (!enrollmentMachine.state.canTalk) return false
        return socket?.send(Codec.listSessions(limit)) == true
    }

    /** Ask the hub what this watch may change, and which models it is allowed to pick. */
    fun requestWatchSettings() {
        socket?.send(Codec.getSettings())
        socket?.send(Codec.listModels())
    }

    fun chooseModel(key: String) {
        socket?.send(Codec.setSetting("model", key))
    }

    fun requestRecap(sessionId: String) {
        socket?.send(Codec.recap(sessionId))
    }

    fun chooseCharacter(id: String) {
        socket?.send(Codec.setSetting("character", id))
    }

    fun selectMode(mode: String, sessionId: String? = null, title: String? = null): Boolean {
        if (!enrollmentMachine.state.canTalk) return false
        val payload = try {
            Codec.setMode(mode, sessionId)
        } catch (_: IllegalArgumentException) {
            return false
        }
        val sent = socket?.send(payload) == true
        if (sent) {
            PocketWearState.mode.value = Codec.ModeMessage(
                mode = mode,
                sessionId = if (mode == Codec.MODE_SESSION) sessionId else null,
                title = if (mode == Codec.MODE_SESSION) title else null
            )
        }
        return sent
    }

    fun cycleModeSlot(direction: Int): Boolean {
        if (!enrollmentMachine.state.canTalk) return false
        val next = Codec.cycleSlotMode(
            current = PocketWearState.mode.value,
            slots = PocketWearState.slots.value,
            direction = direction
        )
        val sent = selectMode(mode = next.mode, sessionId = next.sessionId, title = next.title)
        if (sent) {
            buzz("short")
        }
        return sent
    }

    fun setListenOnOpen(enabled: Boolean) {
        PocketWearState.listenOnOpen.value = enabled
        scope.launch { wearStore.edit { it[listenOnOpenKey] = enabled } }
    }

    fun setConversationMode(enabled: Boolean) {
        PocketWearState.conversationMode.value = enabled
        scope.launch { wearStore.edit { it[conversationModeKey] = enabled } }
    }

    fun setConversationTimeoutSec(seconds: Int) {
        val normalized = if (seconds in setOf(10, 20, 30)) seconds else 20
        PocketWearState.conversationTimeoutSec.value = normalized
        scope.launch { wearStore.edit { it[conversationTimeoutSecKey] = normalized } }
    }

    fun setHapticsEnabled(enabled: Boolean) {
        PocketWearState.hapticsEnabled.value = enabled
        scope.launch { wearStore.edit { it[hapticsEnabledKey] = enabled } }
    }

    fun setDeveloperMode(enabled: Boolean) {
        PocketWearState.developerMode.value = enabled
        scope.launch { wearStore.edit { it[developerModeKey] = enabled } }
    }

    fun interrupt() {
        if (!enrollmentMachine.state.canTalk) return
        synchronized(this) {
            audioPlaying = false
            replyPending = false
        }
        PocketWearState.transcriptState.update { it.startNewTurn() }
        playback.interrupt()
        demoteFromPlaybackForeground()
        socket?.send(Codec.interrupt())
        onUsageStateChanged()
    }

    fun retryPairing() {
        enrollmentMachine.onRetry()
        PocketWearState.enrollment.value = enrollmentMachine.state
        connectionPolicy.resetForRetry("retry pairing")
        PocketWearState.connectionGaveUp.value = false
        reconnectNow("retry pairing")
    }

    /**
     * The user is here and wants to talk: if there is no socket, connect NOW (cancel any backoff, forget a give-up).
     * Never leaves the app silently disconnected while visible or while the user presses Talk.
     */
    @Synchronized
    fun ensureConnected(reason: String) {
        if (socket != null) return
        Log.i(TAG, "ensureConnected: $reason (no socket)")
        reconnect?.cancel()
        reconnect = null
        connectionPolicy.resetForRetry(reason)
        PocketWearState.connectionGaveUp.value = false
        inForeground = inForeground || PocketWearState.isActivityInForeground()
        connect(reason)
    }

    /** A turn can't complete (no connection): drop it locally and say so, instead of showing Thinking… forever. */
    @Synchronized
    /** End a turn whose audio died, and make sure playback stops too. */
    fun abandonSilentTurn(message: String) {
        playback.interrupt()
        abandonTurn(message)
    }

    fun abandonTurn(message: String) {
        Log.i(TAG, "abandonTurn: $message")
        replyPending = false
        audioPlaying = false
        // These three left set are why the talk button stopped responding after a reply was
        // cut off: the app still believed it was speaking and still expecting an answer.
        PocketWearState.speakingAudio.value = false
        PocketWearState.expectingReply.value = false
        PocketWearState.lastAudioFrameMs.value = 0L
        PocketWearState.workingCount.value = 0
        PocketWearState.visibleError.value = message
        PocketWearState.screen.value = "idle"
        PocketWearState.serverStateEvents.tryEmit(Codec.StateMessage(screen = "idle"))
        buzz("short")
        onUsageStateChanged()
    }

    @Synchronized
    fun retryConnection() {
        PocketWearState.visibleError.value = null
        connectionPolicy.resetForRetry("tap to retry")
        PocketWearState.connectionGaveUp.value = false
        reconnectNow("tap to retry")
    }

    @Synchronized
    private fun reconnectNow(reason: String) {
        reconnect?.cancel()
        reconnect = null
        stopPingJob()
        connectionPolicy.onDisconnected(reason)
        socket?.close(1000, reason)
        socket = null
        PocketWearState.socketConnected.value = false
        playback.interrupt()
        demoteFromPlaybackForeground()
        connect(reason)
    }

    /**
     * Forget the approved computer and look for one again, so the pairing code is
     * reachable from the watch instead of only from a terminal on the laptop.
     */
    fun pairWithAnotherComputer() {
        stopSearch()
        scope.launch {
            wearStore.edit { p ->
                p.remove(tokenKey)
                p.remove(computerFpKey)
                p.remove(computerNameKey)
            }
            token = null
            savedFp = null
            enrollmentMachine.updateToken(null)
            PocketWearState.enrollment.value = EnrollmentState.Disconnected
            socket?.close(1000, "pairing again")
            socket = null
            PocketWearState.socketConnected.value = false
            startSearch()
        }
    }

    fun settings(newHost: String, resetId: Boolean, forget: Boolean) {
        stopSearch()
        scope.launch {
            val trimmed = newHost.trim().ifEmpty { ConnectionTracker.DEFAULT_HOST }
            wearStore.edit { p ->
                p[hostKey] = trimmed
                p.remove(computerFpKey)
                p.remove(computerNameKey)
                if (resetId) {
                    p[idKey] = UUID.randomUUID().toString()
                }
                if (forget || resetId || trimmed != host) {
                    p.remove(tokenKey)
                }
            }
            val p = wearStore.data.first()
            host = p[hostKey] ?: ConnectionTracker.DEFAULT_HOST
            hasSavedHost = true
            savedFp = null
            id = p[idKey]!!
            PocketWearState.deviceId.value = id
            token = p[tokenKey]
            enrollmentMachine.updateToken(token)
            connectionPolicy.resetForRetry("settings changed")
            PocketWearState.connectionGaveUp.value = false
            updateWifiRequestForForegroundState()
            reconnectNow("settings changed")
        }
    }

    @Synchronized
    fun startSearch() {
        if (!inForeground || searchJob != null) return
        reconnect?.cancel()
        reconnect = null
        searchMachine.start(android.os.SystemClock.elapsedRealtime())
        PocketWearState.discovery.value = searchMachine.state
        updateWifiRequestForForegroundState()
        searchJob = scope.launch(start = CoroutineStart.LAZY) {
            var lock: WifiManager.MulticastLock? = null
            try {
                val wifi = applicationContext.getSystemService(WifiManager::class.java)
                lock = wifi?.createMulticastLock("hermes-discovery")?.apply { setReferenceCounted(false); acquire() }
                DatagramSocket(null).use { udp ->
                    udp.reuseAddress = true
                    udp.bind(java.net.InetSocketAddress(8768))
                    udp.soTimeout = 250
                    val started = android.os.SystemClock.elapsedRealtime()
                    val computers = linkedMapOf<String, Computer>()
                    while (isActive && android.os.SystemClock.elapsedRealtime() - started < SearchMachine.TIMEOUT_MS) {
                        try {
                            val packet = DatagramPacket(ByteArray(4096), 4096)
                            udp.receive(packet)
                            val computer = Discovery.parse(String(packet.data, packet.offset, packet.length, Charsets.UTF_8), packet.address.hostAddress ?: "")
                            if (computer != null) computers[computer.fp] = computer
                        } catch (_: SocketTimeoutException) { }
                        val preferred = Discovery.preferred(computers.values.toList(), savedFp)
                        if (preferred != null) { selectComputer(preferred); return@launch }
                        if (android.os.SystemClock.elapsedRealtime() - started >= 2000 && computers.isNotEmpty()) {
                            if (computers.size == 1 && savedFp == null) { selectComputer(computers.values.first()); return@launch }
                            synchronized(this@PocketWearService) {
                                if (isActive) {
                                    searchMachine.choices(computers.values.toList())
                                    PocketWearState.discovery.value = searchMachine.state
                                }
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                if (e !is CancellationException) Log.w(TAG, "Discovery failed", e)
            } finally {
                try { if (lock?.isHeld == true) lock.release() } catch (_: Exception) { }
                synchronized(this@PocketWearService) {
                    if (searchJob === coroutineContext[Job]) {
                        searchJob = null
                        if (isActive && inForeground) {
                            searchMachine.fail()
                            PocketWearState.discovery.value = searchMachine.state
                            updateWifiRequestForForegroundState()
                        }
                    }
                }
            }
        }
        searchJob?.start()
    }

    @Synchronized
    private fun stopSearch() {
        searchJob?.cancel()
        searchJob = null
        searchMachine.stop()
        PocketWearState.discovery.value = searchMachine.state
        updateWifiRequestForForegroundState()
    }

    @Synchronized
    fun selectComputer(computer: Computer) {
        if (!inForeground) return
        val changed = savedFp != computer.fp
        stopSearch()
        host = computer.address
        hasSavedHost = true
        savedFp = computer.fp
        if (changed) { token = null; enrollmentMachine.updateToken(null) }
        scope.launch {
            wearStore.edit { p ->
                p[hostKey] = computer.address
                p[computerNameKey] = computer.name
                p[computerFpKey] = computer.fp
                if (changed) p.remove(tokenKey)
            }
            synchronized(this@PocketWearService) {
                if (inForeground && host == computer.address && savedFp == computer.fp) {
                    connectionPolicy.resetForRetry("computer selected")
                    updateWifiRequestForForegroundState()
                    reconnectNow("computer selected")
                }
            }
        }
    }

    fun currentHost(): String = host

    override fun onDestroy() {
        stopSearch()
        idleDisconnectJob?.cancel()
        stopPingJob()
        connectionPolicy.onDisconnected("service stopped")
        reconnect?.cancel()
        releaseWifiNetwork()
        demoteFromPlaybackForeground()
        playback.close()
        socket?.close(1000, "service stopped")
        PocketWearState.socketConnected.value = false
        scope.cancel()
        baseClient.dispatcher.executorService.shutdown()
        PocketWearState.service = null
        super.onDestroy()
    }
}
