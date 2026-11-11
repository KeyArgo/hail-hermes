package com.hermes.pocket.wear

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.core.*
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.wear.compose.foundation.lazy.ScalingLazyColumn
import androidx.wear.compose.foundation.lazy.items
import androidx.wear.compose.foundation.lazy.rememberScalingLazyListState
import androidx.wear.compose.material.*
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch

private data class WatchMetrics(
    val widthDp: Float,
    val heightDp: Float,
    val isRound: Boolean
) {
    val minSideDp: Float = minOf(widthDp, heightDp)
    fun w(fraction: Float): Dp = (widthDp * fraction).dp
    fun h(fraction: Float): Dp = (heightDp * fraction).dp
    fun min(fraction: Float): Dp = (minSideDp * fraction).dp
    fun sp(fraction: Float): TextUnit = (widthDp * fraction).sp

    val horizontalPad: Dp get() = w(if (isRound) 0.10f else 0.05f)
    val topPad: Dp get() = h(if (isRound) 0.12f else 0.07f)
    val bottomPad: Dp get() = h(if (isRound) 0.16f else 0.09f)
    val itemSpacing: Dp get() = h(0.022f)
    val captionSp: TextUnit get() = sp(0.052f)
    val smallSp: TextUnit get() = sp(0.058f)
    val bodySp: TextUnit get() = sp(0.064f)
    val talkSp: TextUnit get() = sp(0.074f)
    val pairCodeSp: TextUnit get() = sp(0.18f)
}

class MainActivity : ComponentActivity() {
    private val activityScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val talkStateFlow = MutableStateFlow(TalkState.IDLE)
    private val conversationActiveFlow = MutableStateFlow(false)
    private val activityVisibleFlow = MutableStateFlow(false)
    private val voiceActivity = VoiceActivity()
    private var micRecorder: ActivityMicRecorder? = null
    private var pendingLaunchListen: Boolean = false

    private val talkTransport = object : TalkTransport {
        override fun startRecorder(): Boolean {
            val enrolled = PocketWearState.enrollment.value == EnrollmentState.Ready
            val recorder = micRecorder
            if (!enrolled) {
                Log.i("HailHermes", "startRecorder: not ready (enrollment=${PocketWearState.enrollment.value})")
                return false
            }
            if (recorder == null) {
                Log.i("HailHermes", "startRecorder: no recorder yet")
                return false
            }
            // Pressing Talk always (re)connects if needed; frames recorded meanwhile are buffered by TalkController.
            PocketWearState.service?.ensureConnected("talk pressed")
            PocketWearState.service?.onUserListenStart()
            val started = recorder.start()
            Log.i("HailHermes", "startRecorder: started=$started")
            return started
        }

        override fun stopRecorder() {
            micRecorder?.stop()
        }

        override fun sendAudioStart(): Boolean {
            return PocketWearState.service?.sendAudioStart() == true
        }

        override fun sendAudioFrame(pcm: ByteArray): Boolean {
            return PocketWearState.service?.sendAudioFrame(pcm) == true
        }

        override fun sendAudioEnd(): Boolean {
            return PocketWearState.service?.sendAudioEnd() == true
        }

        override fun onUndelivered() {
            PocketWearState.service?.abandonTurn(PocketWearService.CANT_REACH)
        }
    }

    private val talkController = TalkController(
        transport = talkTransport,
        onStateChanged = { newState ->
            talkStateFlow.value = newState
        }
    )

    private val permissions = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { tryStartListenOnOpen() }

    private fun ensureServiceStarted() {
        try {
            startService(Intent(this, PocketWearService::class.java))
        } catch (_: Exception) {
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        pendingLaunchListen = true
        ensureServiceStarted()

        micRecorder = ActivityMicRecorder(
            scope = activityScope,
            onFrame = { pcm ->
                val now = System.currentTimeMillis()
                talkController.onPcmFrame(pcm, now)
                if (voiceActivity.isActive) {
                    when (voiceActivity.onPcmFrame(pcm, now)) {
                        VoiceDecision.END_OF_SPEECH -> activityScope.launch {
                            talkController.endHold(System.currentTimeMillis())
                            conversationActiveFlow.value = voiceActivity.isActive
                            PocketWearState.setExpectingReply(voiceActivity.isReplyPending)
                        }
                        VoiceDecision.SILENCE_TIMEOUT -> activityScope.launch {
                            talkController.reset()
                            micRecorder?.stop()
                            stopConversationMode(softHaptic = true)
                        }
                        VoiceDecision.CONTINUE -> {}
                    }
                }
            },
            onPermissionDenied = {
                PocketWearState.status.value = "Mic perm needed"
            }
        )

        activityScope.launch {
            PocketWearState.serverStateEvents.collect { stateEvent ->
                val screenState = stateEvent.screen
                val wasActive = voiceActivity.isActive
                talkController.onServerState(screenState)
                if (wasActive &&
                    PocketWearState.isActivityInForeground() &&
                    voiceActivity.onServerScreen(
                        screen = screenState,
                        nowMs = System.currentTimeMillis(),
                        conversationEnd = stateEvent.conversationEnd
                    )
                ) {
                    restartConversationListening()
                } else if (wasActive && !voiceActivity.isActive) {
                    stopConversationMode(softHaptic = true)
                }
                conversationActiveFlow.value = voiceActivity.isActive
                PocketWearState.setExpectingReply(voiceActivity.isReplyPending)
            }
        }

        activityScope.launch {
            PocketWearState.replyAudioEvents.collect { event ->
                if (event == "audio_start" && voiceActivity.isActive) {
                    voiceActivity.onReplyAudioStart()
                } else if (event == "audio_end") {
                    val wasActive = voiceActivity.isActive
                    if (wasActive &&
                        PocketWearState.isActivityInForeground() &&
                        voiceActivity.onReplyAudioEnd(System.currentTimeMillis())
                    ) {
                        restartConversationListening()
                    } else if (wasActive && !voiceActivity.isActive) {
                        stopConversationMode(softHaptic = true)
                    }
                    conversationActiveFlow.value = voiceActivity.isActive
                    PocketWearState.setExpectingReply(voiceActivity.isReplyPending)
                }
            }
        }

        activityScope.launch {
            PocketWearState.enrollment.collect { enrollment ->
                if (!enrollment.canTalk) {
                    voiceActivity.stopConversation()
                    conversationActiveFlow.value = false
                    talkController.reset()
                } else {
                    tryStartListenOnOpen()
                }
            }
        }

        activityScope.launch {
            PocketWearState.prefsLoaded.collect { if (it) tryStartListenOnOpen() }
        }

        activityScope.launch {
            PocketWearState.conversationTimeoutSec.collect { seconds ->
                voiceActivity.maxSilenceMs = seconds.coerceIn(10, 30) * 1_000L
            }
        }

        val requested = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 33) {
            requested.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        permissions.launch(requested.toTypedArray())

        setContent {
            MaterialTheme {
                WearApp()
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        pendingLaunchListen = true
        tryStartListenOnOpen()
    }

    override fun onStart() {
        super.onStart()
        activityVisibleFlow.value = true
        ensureServiceStarted()
        PocketWearState.setActivityInForeground(true)
        tryStartListenOnOpen()
    }

    override fun onResume() {
        super.onResume()
        activityVisibleFlow.value = true
        // Waking from the dim always-on screen only resumes us (no onStart), so mark the app foreground here too,
        // otherwise the service thinks we're in the background and refuses to connect (the "frozen Thinking" bug).
        PocketWearState.setActivityInForeground(true)
        PocketWearState.service?.ensureConnected("app visible")
        tryStartListenOnOpen()
    }

    override fun onPause() {
        activityVisibleFlow.value = false
        pendingLaunchListen = false
        voiceActivity.stopConversation()
        conversationActiveFlow.value = false
        PocketWearState.conversationMode.value = false
        PocketWearState.setExpectingReply(false)
        talkController.onPause()
        micRecorder?.stop()
        updateKeepScreenOn(false)
        super.onPause()
    }

    override fun onStop() {
        activityVisibleFlow.value = false
        updateKeepScreenOn(false)
        PocketWearState.setActivityInForeground(false)
        super.onStop()
    }

    override fun onDestroy() {
        activityVisibleFlow.value = false
        voiceActivity.stopConversation()
        PocketWearState.setExpectingReply(false)
        micRecorder?.stop()
        updateKeepScreenOn(false)
        activityScope.cancel()
        super.onDestroy()
    }

    private fun hasAudioPermission(): Boolean = ContextCompat.checkSelfPermission(
        this, Manifest.permission.RECORD_AUDIO
    ) == PackageManager.PERMISSION_GRANTED

    private fun stopConversationMode(softHaptic: Boolean) {
        val wasActive = voiceActivity.isActive || conversationActiveFlow.value
        voiceActivity.stopConversation()
        conversationActiveFlow.value = false
        PocketWearState.conversationMode.value = false
        PocketWearState.setExpectingReply(false)
        if (wasActive && softHaptic) {
            PocketWearState.service?.buzz("soft")
        }
    }

    private fun updateKeepScreenOn(keepOn: Boolean) {
        if (keepOn) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    private fun tryStartListenOnOpen() {
        if (!pendingLaunchListen || !PocketWearState.prefsLoaded.value) return
        if (!PocketWearState.isActivityInForeground() || !activityVisibleFlow.value) return
        val shouldListen = intent?.action == Intent.ACTION_ASSIST || PocketWearState.listenOnOpen.value
        if (!shouldListen) {
            pendingLaunchListen = false
            return
        }
        if (PocketWearState.enrollment.value != EnrollmentState.Ready || !hasAudioPermission()) return
        pendingLaunchListen = false
        if (talkController.state == TalkState.IDLE) {
            val now = System.currentTimeMillis()
            voiceActivity.startListeningTurn(now)
            if (talkController.startHold(now)) {
                conversationActiveFlow.value = true
                PocketWearState.service?.buzz("short")
            } else {
                voiceActivity.stopConversation()
                conversationActiveFlow.value = false
            }
        }
    }

    private fun restartConversationListening() {
        activityScope.launch {
            delay(200L)
            if (!PocketWearState.isActivityInForeground() ||
                !voiceActivity.isActive ||
                !PocketWearState.enrollment.value.canTalk
            ) return@launch
            if (talkController.state == TalkState.IDLE) {
                val now = System.currentTimeMillis()
                voiceActivity.startListeningTurn(now)
                if (talkController.startHold(now)) {
                    conversationActiveFlow.value = true
                } else {
                    stopConversationMode(softHaptic = true)
                }
            }
        }
    }

    @Composable
    fun WearApp() {
        val status by PocketWearState.status.collectAsState()
        val state by PocketWearState.screen.collectAsState()
        val enrollment by PocketWearState.enrollment.collectAsState()
        val discovery by PocketWearState.discovery.collectAsState()
        val transcript by PocketWearState.transcript.collectAsState()
        val currentMode by PocketWearState.mode.collectAsState()
        val sessions by PocketWearState.sessions.collectAsState()
        val slots by PocketWearState.slots.collectAsState()
        val listenOnOpen by PocketWearState.listenOnOpen.collectAsState()
        val conversationTimeoutSec by PocketWearState.conversationTimeoutSec.collectAsState()
        val hapticsEnabled by PocketWearState.hapticsEnabled.collectAsState()
        val developerMode by PocketWearState.developerMode.collectAsState()
        val lastTurnTiming by PocketWearState.lastTurnTiming.collectAsState()
        val deviceId by PocketWearState.deviceId.collectAsState()
        val volumeOffHint by PocketWearState.volumeOffHint.collectAsState()
        val workingCount by PocketWearState.workingCount.collectAsState()
        val expectingReply by PocketWearState.expectingReply.collectAsState()
        val visibleError by PocketWearState.visibleError.collectAsState()
        val recapNotice by PocketWearState.recapNotice.collectAsState()
        val responseEvents by PocketWearState.responseEvents.collectAsState()
        val activityVisible by activityVisibleFlow.collectAsState()
        val talkState by talkStateFlow.collectAsState()
        val conversationActive by conversationActiveFlow.collectAsState()
        val watchModels by PocketWearState.watchModels.collectAsState()
        val currentSkin by Codec.characterFlow.collectAsState()
        val watchModelNow by PocketWearState.watchModel.collectAsState()
        val character by Codec.characterFlow.collectAsState()

        var showSettings by remember { mutableStateOf(false) }
        var showModePicker by remember { mutableStateOf(false) }
        var host by remember(showSettings) {
            mutableStateOf(
                PocketWearState.service?.currentHost() ?: ConnectionTracker.DEFAULT_HOST
            )
        }
        val listState = rememberScalingLazyListState(initialCenterItemIndex = 0)
        val isListening = talkState == TalkState.LISTENING || state == "listening"
        val effectiveState = when {
            isListening -> "listening"
            state == "speaking" -> "speaking"
            talkState == TalkState.SENDING || state == "thinking" || workingCount > 0 -> "thinking"
            else -> state
        }
        val thinking = effectiveState == "thinking"
        // Anything that means "Hermes still owes you an answer". The owner must always be
        // able to see that work is happening, even while the screen still reads idle.
        // After an utterance the controller sits in SENDING until something resets it, and
        // while it sits there the button is "busy" and every new press is ignored. Free it as
        // soon as the turn is really over: nothing playing, nothing owed, hub back to idle.
        LaunchedEffect(effectiveState, expectingReply, talkState) {
            if (talkState == TalkState.SENDING && effectiveState == "idle" && !expectingReply &&
                !PocketWearState.speakingAudio.value
            ) {
                delay(400)
                if (PocketWearState.screen.value == "idle" && !PocketWearState.expectingReply.value) {
                    talkController.reset()
                }
            }
        }

        val busyNow = workingCount > 0 || expectingReply ||
            talkState == TalkState.SENDING || effectiveState == "thinking" || effectiveState == "speaking"
        val keepAwake = activityVisible && (isListening || conversationActive ||
            talkState == TalkState.SENDING || state == "thinking" || state == "speaking" || workingCount > 0 ||
            // A pairing code must stay readable: the owner leaves the watch to approve it.
            enrollment is EnrollmentState.Pending)
        var stillWorking by remember { mutableStateOf(false) }
        val latestResponseEvents by rememberUpdatedState(responseEvents)
        val scrollEnabled = !talkController.isScrollLocked && talkState != TalkState.LISTENING
        val config = LocalConfiguration.current
        var horizontalDragTotal by remember { mutableFloatStateOf(0f) }

        BackHandler(enabled = showSettings || showModePicker) {
            if (showSettings) {
                showSettings = false
            } else if (showModePicker) {
                showModePicker = false
            }
        }

        LaunchedEffect(showSettings, showModePicker) {
            listState.scrollToItem(0)
        }

        // Screen stays on for the whole turn. Waiting/thinking is capped at 130 s (above the 120 s Thinking ceiling); while Hermes is SPEAKING the timer restarts
        // and holds until the reply ends (10 min safety cap), because Android starves our audio once the screen sleeps.
        val speakingNow = state == "speaking"
        // Wear sleeps the screen a few seconds after the last input, and a press on a
        // sleeping screen only wakes the watch — the button never sees it. That is why the
        // first question worked and every later one needed several tries. Keep the screen
        // alive while anything is happening and for two minutes afterwards, so a second
        // question lands on the button; when the app goes quiet it sleeps as usual.
        val keepAlive = keepAwake || speakingNow
        LaunchedEffect(keepAlive, busyNow, isListening, state) {
            updateKeepScreenOn(true)
            delay(if (keepAlive) 600_000L else 120_000L)
            updateKeepScreenOn(false)
        }
        DisposableEffect(Unit) {
            onDispose { updateKeepScreenOn(false) }
        }
        LaunchedEffect(thinking) {
            stillWorking = false
            if (thinking) {
                PocketWearState.service?.buzz("tick")
                val startingResponseEvents = latestResponseEvents
                delay(45_000L)
                stillWorking = latestResponseEvents == startingResponseEvents
            }
        }
        LaunchedEffect(responseEvents) { stillWorking = false }
        // Never "Thinking…" without a connection: after 8 s with no socket, give up the turn and say so.
        val socketConnected by PocketWearState.socketConnected.collectAsState()
        LaunchedEffect(thinking, socketConnected) {
            if (thinking && !socketConnected) {
                PocketWearState.service?.ensureConnected("thinking without socket")
                delay(8_000L)
                if (!PocketWearState.socketConnected.value) {
                    talkController.reset()
                    PocketWearState.service?.abandonTurn(PocketWearService.CANT_REACH)
                }
            }
        }

        // Never "Thinking…" forever: if the hub is alive but wedged, the socket stays open
        // and the 8 s no-socket watchdog above never fires. Two minutes with no response is
        // the ceiling; the hub's own 120 s turn ceiling cannot help when its loop is stuck.
        LaunchedEffect(thinking, responseEvents) {
            if (!thinking) return@LaunchedEffect
            val startingEvents = latestResponseEvents
            delay(120_000L)
            if (latestResponseEvents == startingEvents && PocketWearState.screen.value == "thinking") {
                talkController.reset()
                PocketWearState.service?.abandonTurn("No reply after two minutes — try again")
            }
        }
        // Never "Speaking…" forever: if the hub dies while audio frames are in flight,
        // audio_end never arrives. Watch the frame heartbeat rather than the label.
        val audioSpeaking by PocketWearState.speakingAudio.collectAsState()
        LaunchedEffect(audioSpeaking) {
            while (PocketWearState.speakingAudio.value) {
                delay(3_000L)
                val silentMs = android.os.SystemClock.elapsedRealtime() -
                    PocketWearState.lastAudioFrameMs.value
                // 20s, not 12s: a slow multi-chunk TTS can legitimately pause between
                // chunks, and cutting a real reply is worse than a slow escape.
                if (PocketWearState.speakingAudio.value && silentMs > 20_000L) {
                    // Only a real outage is worth an error. If the turn already finished and
                    // only this flag was left behind, clear it silently — telling someone
                    // "speech stopped unexpectedly" after a completed answer is a lie.
                    val stillWaiting = PocketWearState.expectingReply.value ||
                        PocketWearState.screen.value != "idle"
                    if (!stillWaiting) {
                        PocketWearState.speakingAudio.value = false
                        return@LaunchedEffect
                    }
                    talkController.reset()
                    PocketWearState.service?.abandonSilentTurn("Speech stopped unexpectedly")
                    return@LaunchedEffect
                }
            }
        }

        BoxWithConstraints(
            modifier = Modifier
                .fillMaxSize()
                .background(Color.Black)
                .pointerInput(enrollment, showSettings, showModePicker, isListening, slots) {
                    if (isListening || (!enrollment.canTalk && !showSettings && !showModePicker)) {
                        return@pointerInput
                    }
                    // Edge-only. This detector used to cover the whole screen and competed
                    // with the hold-to-talk button, which is why presses were "hit and hope".
                    var armed = false
                    detectHorizontalDragGestures(
                        onDragStart = { offset ->
                            horizontalDragTotal = 0f
                            val w = size.width.toFloat().coerceAtLeast(1f)
                            armed = offset.x < w * 0.18f || offset.x > w * 0.82f
                        },
                        onHorizontalDrag = { _, dragAmount ->
                            if (armed) horizontalDragTotal += dragAmount
                        },
                        onDragEnd = {
                            if (!armed) {
                                horizontalDragTotal = 0f
                                return@detectHorizontalDragGestures
                            }
                            val thresholdPx = size.width * 0.18f
                            if (showSettings || showModePicker) {
                                if (horizontalDragTotal >= thresholdPx) {
                                    showSettings = false
                                    showModePicker = false
                                }
                            } else if (enrollment.canTalk && kotlin.math.abs(horizontalDragTotal) >= thresholdPx) {
                                val direction = if (horizontalDragTotal < 0f) 1 else -1
                                PocketWearState.service?.cycleModeSlot(direction)
                            }
                            horizontalDragTotal = 0f
                        }
                    )
                }
        ) {
            val wDp = if (maxWidth.value > 0f) maxWidth.value else config.screenWidthDp.toFloat()
            val hDp = if (maxHeight.value > 0f) maxHeight.value else config.screenHeightDp.toFloat()
            val m = remember(wDp, hDp, config.isScreenRound) {
                WatchMetrics(wDp, hDp, config.isScreenRound)
            }

            Scaffold(
                timeText = { TimeText() },
                vignette = { Vignette(vignettePosition = VignettePosition.TopAndBottom) },
                positionIndicator = { PositionIndicator(scalingLazyListState = listState) }
            ) {
                ScalingLazyColumn(
                    state = listState,
                    autoCentering = null,
                    userScrollEnabled = scrollEnabled,
                    modifier = Modifier
                        .fillMaxSize()
                        .background(Color.Black),
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(m.itemSpacing),
                    contentPadding = PaddingValues(
                        top = m.topPad,
                        bottom = m.bottomPad,
                        start = m.horizontalPad,
                        end = m.horizontalPad
                    )
                ) {
                    val currentEnrollment = enrollment
                    if (discovery !is SearchState.Idle) {
                        when (val search = discovery) {
                            SearchState.Looking -> item {
                                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                    CircularProgressIndicator(modifier = Modifier.size(m.min(0.20f)))
                                    Spacer(Modifier.height(m.h(0.04f)))
                                    Text("Looking for your computer…", fontSize = m.bodySp, textAlign = TextAlign.Center)
                                }
                            }
                            is SearchState.Choices -> {
                                item { Text("Choose your computer", fontSize = m.bodySp, textAlign = TextAlign.Center) }
                                items(search.computers) { computer ->
                                    CompactChip(onClick = { PocketWearState.service?.selectComputer(computer) },
                                        label = { Text(computer.name, fontSize = m.bodySp) })
                                }
                            }
                            SearchState.NotFound -> item {
                                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                    Text("Put your watch and computer on the same Wi-Fi", fontSize = m.bodySp, textAlign = TextAlign.Center)
                                    Spacer(Modifier.height(m.h(0.03f)))
                                    CompactChip(onClick = { PocketWearState.service?.startSearch() }, label = { Text("Try again", fontSize = m.bodySp) })
                                }
                            }
                            SearchState.Idle -> Unit
                        }
                    } else if (currentEnrollment is EnrollmentState.Pending) {
                        item {
                            var nowMillis by remember(currentEnrollment) {
                                mutableLongStateOf(System.currentTimeMillis())
                            }
                            LaunchedEffect(currentEnrollment) {
                                while (true) {
                                    nowMillis = System.currentTimeMillis()
                                    delay(1_000L)
                                }
                            }
                            val secondsLeft = currentEnrollment.secondsRemaining(nowMillis)
                            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                Text(
                                    text = currentEnrollment.matchCode,
                                    fontSize = m.pairCodeSp,
                                    fontWeight = FontWeight.Bold,
                                    color = Color.White,
                                    textAlign = TextAlign.Center
                                )
                                Spacer(Modifier.height(m.h(0.015f)))
                                Text(
                                    text = "Approve on your computer",
                                    fontSize = m.bodySp,
                                    color = Color(0xFFFFD54F),
                                    textAlign = TextAlign.Center
                                )
                                Spacer(Modifier.height(m.h(0.01f)))
                                Text(
                                    text = "${secondsLeft}s left",
                                    fontSize = m.smallSp,
                                    color = Color(0xFFB0BEC5),
                                    textAlign = TextAlign.Center
                                )
                                Spacer(Modifier.height(m.h(0.02f)))
                                MascotCanvas(
                                    state = "idle",
                                    modifier = Modifier.size(m.min(0.24f)),
                                    character = character
                                )
                            }
                        }
                    } else if (showModePicker && currentEnrollment.canTalk) {
                        item {
                            MascotCanvas(
                                state = effectiveState,
                                modifier = Modifier.size(m.min(0.20f)),
                                character = character
                            )
                        }
                        item {
                            Chip(
                                onClick = {
                                    PocketWearState.service?.selectMode(Codec.MODE_QUICK)
                                    showModePicker = false
                                },
                                modifier = Modifier.fillMaxWidth(),
                                colors = ChipDefaults.chipColors(
                                    backgroundColor = if (currentMode.mode == Codec.MODE_QUICK) {
                                        Color(0xFF00ACC1)
                                    } else Color(0xFF1C262B)
                                ),
                                label = {
                                    Text(
                                        text = "Quick ask",
                                        fontSize = m.smallSp,
                                        color = if (currentMode.mode == Codec.MODE_QUICK) Color.Black else Color.White,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis
                                    )
                                }
                            )
                        }
                        items(sessions) { session ->
                            val selected = currentMode.mode == Codec.MODE_SESSION &&
                                currentMode.sessionId == session.id
                            Chip(
                                onClick = {
                                    PocketWearState.service?.selectMode(
                                        mode = Codec.MODE_SESSION,
                                        sessionId = session.id,
                                        title = session.title
                                    )
                                    PocketWearState.service?.requestRecap(session.id)
                                    showModePicker = false
                                },
                                modifier = Modifier.fillMaxWidth(),
                                colors = ChipDefaults.chipColors(
                                    backgroundColor = if (selected) Color(0xFF00ACC1) else Color(0xFF1C262B)
                                ),
                                label = {
                                    Text(
                                        text = session.title,
                                        fontSize = m.smallSp,
                                        color = if (selected) Color.Black else Color.White,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis
                                    )
                                },
                                secondaryLabel = if (session.age.isNotBlank() || session.folder.isNotBlank()) {
                                    {
                                        Text(
                                            text = listOf(session.folder, session.age)
                                                .filter { it.isNotBlank() }
                                                .joinToString(" · "),
                                            fontSize = m.captionSp,
                                            color = if (selected) Color(0xFF263238) else Color(0xFFB0BEC5),
                                            maxLines = 1,
                                            overflow = TextOverflow.Ellipsis
                                        )
                                    }
                                } else null
                            )
                        }
                        item {
                            CompactChip(
                                onClick = { showModePicker = false },
                                label = { Text("Back", fontSize = m.captionSp) }
                            )
                        }
                    } else if (showSettings) {
                        item {
                            CompactChip(
                                onClick = { showSettings = false },
                                label = { Text("✓ Done", fontSize = m.captionSp) }
                            )
                        }
                        item {
                            Text(
                                text = "Settings",
                                fontSize = m.bodySp,
                                fontWeight = FontWeight.Bold,
                                color = Color.White,
                                textAlign = TextAlign.Center
                            )
                        }
                        item {
                            CompactChip(
                                onClick = { PocketWearState.service?.setListenOnOpen(!listenOnOpen) },
                                label = {
                                    Text(
                                        text = "Listen on open: ${if (listenOnOpen) "On" else "Off"}",
                                        fontSize = m.captionSp
                                    )
                                }
                            )
                        }
                        item {
                            CompactChip(
                                onClick = {
                                    val next = when (conversationTimeoutSec) {
                                        10 -> 20
                                        20 -> 30
                                        else -> 10
                                    }
                                    PocketWearState.service?.setConversationTimeoutSec(next)
                                },
                                label = {
                                    Text(
                                        text = "Conv timeout: ${conversationTimeoutSec}s",
                                        fontSize = m.captionSp
                                    )
                                }
                            )
                        }
                        item {
                            CompactChip(
                                onClick = { PocketWearState.service?.setHapticsEnabled(!hapticsEnabled) },
                                label = {
                                    Text(
                                        text = "Haptics: ${if (hapticsEnabled) "On" else "Off"}",
                                        fontSize = m.captionSp
                                    )
                                }
                            )
                        }
                        item {
                            CompactChip(
                                onClick = { PocketWearState.service?.setDeveloperMode(!developerMode) },
                                label = {
                                    Text(
                                        text = "Developer mode: ${if (developerMode) "On" else "Off"}",
                                        fontSize = m.captionSp
                                    )
                                }
                            )
                        }
                        if (developerMode) {
                            item {
                                Text(
                                    text = "ID: ${deviceId.take(8)}",
                                    fontSize = m.captionSp,
                                    color = Color(0xFFB0BEC5),
                                    textAlign = TextAlign.Center
                                )
                            }
                            item {
                                OutlinedTextField(
                                    value = host,
                                    onValueChange = { host = it },
                                    label = { Text("Hub host:port", fontSize = m.captionSp) },
                                    singleLine = true,
                                    modifier = Modifier.fillMaxWidth()
                                )
                            }
                            item {
                                CompactChip(
                                    onClick = {
                                        PocketWearState.service?.settings(host, resetId = false, forget = false)
                                        showSettings = false
                                    },
                                    label = { Text("Save & Reconnect", fontSize = m.captionSp) }
                                )
                            }
                        }
                        item {
                            CompactChip(
                                onClick = {
                                    PocketWearState.service?.pairWithAnotherComputer()
                                    showSettings = false
                                },
                                label = { Text("Pair a new computer", fontSize = m.captionSp) }
                            )
                        }
                        item {
                            Text(text = "Character", fontSize = m.captionSp, color = Color(0xFFB0BEC5))
                        }
                        items(listOf(
                            "nous-warm" to "Nous",
                            "nous-white" to "White ring",
                            "nous-soft" to "Grey ring",
                            "nous-teal" to "Teal ring",
                            "nous-dark" to "Dark",
                            "nous-light" to "Light",
                            "hermes" to "Hermes"
                        )) { (id, label) ->
                            val picked = currentSkin == id
                            CompactChip(
                                onClick = { PocketWearState.service?.chooseCharacter(id) },
                                label = {
                                    Text(
                                        text = if (picked) "OK  $label" else label,
                                        fontSize = m.captionSp
                                    )
                                }
                            )
                        }
                        if (watchModels.isNotEmpty()) {
                            item {
                                Text(
                                    text = "Model",
                                    fontSize = m.captionSp,
                                    color = Color(0xFFB0BEC5)
                                )
                            }
                            items(watchModels) { (key, label) ->
                                val chosen = key == watchModelNow
                                CompactChip(
                                    onClick = { PocketWearState.service?.chooseModel(key) },
                                    label = {
                                        Text(
                                            text = if (chosen) "OK  $label" else label,
                                            fontSize = m.captionSp,
                                            maxLines = 1,
                                            overflow = TextOverflow.Ellipsis
                                        )
                                    }
                                )
                            }
                        }
                        if (developerMode) {
                            item {
                                CompactChip(
                                    onClick = {
                                        PocketWearState.service?.settings(host, resetId = true, forget = true)
                                        showSettings = false
                                    },
                                    label = { Text("Reset Watch ID", fontSize = m.captionSp) }
                                )
                            }
                        }
                        item {
                            CompactChip(
                                onClick = { showSettings = false },
                                label = { Text("Back", fontSize = m.captionSp) }
                            )
                        }
                    } else {
                        item {
                            Surface(
                                color = Color(0xFF1C262B),
                                shape = RoundedCornerShape(percent = 50),
                                modifier = Modifier
                                    .fillMaxWidth(if (m.isRound) 0.68f else 0.88f)
                                    .clickable(enabled = currentEnrollment.canTalk) {
                                        PocketWearState.service?.requestSessions()
                                        showModePicker = true
                                    }
                            ) {
                                Text(
                                    text = currentMode.displayLabel,
                                    fontSize = m.captionSp,
                                    color = Color(0xFF80DEEA),
                                    textAlign = TextAlign.Center,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                    modifier = Modifier.padding(
                                        horizontal = m.w(0.05f),
                                        vertical = m.h(0.018f)
                                    )
                                )
                            }
                        }

                        item {
                            MascotCanvas(
                                state = effectiveState,
                                modifier = Modifier
                                    .fillMaxWidth(if (m.isRound) 0.62f else 0.72f)
                                    .height(m.h(0.32f)),
                                character = character
                            )
                        }

                        if (currentEnrollment is EnrollmentState.Failed) {
                            item {
                                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                    Text(
                                        text = currentEnrollment.message,
                                        fontSize = m.smallSp,
                                        color = Color(0xFFFF8A80),
                                        textAlign = TextAlign.Center
                                    )
                                    Spacer(Modifier.height(m.h(0.015f)))
                                    CompactChip(
                                        onClick = { PocketWearState.service?.retryPairing() },
                                        label = { Text("Retry", fontSize = m.smallSp) }
                                    )
                                }
                            }
                        }

                        if (currentEnrollment.canTalk) {
                            item {
                                val buttonColor = when (effectiveState) {
                                    "listening" -> Color(0xFF00C853)
                                    "thinking" -> Color(0xFFFFD54F)
                                    "speaking" -> Color(0xFF40C4FF)
                                    else -> if (busyNow) Color(0xFFFFD54F)
                                    else if (conversationActive) Color(0xFF26A69A) else Color(0xFF00ACC1)
                                }
                                val buttonText = when (effectiveState) {
                                    "listening" -> "Listening…"
                                    "thinking" -> "Thinking…"
                                    "speaking" -> "Speaking…"
                                    // Never look idle while Hermes is still working: the owner
                                    // has waited on a silent screen before.
                                    else -> if (busyNow) "Working…"
                                    else if (conversationActive) "Tap to stop"
                                    // Say what the button does: hold it and talk. A bare
                                    // "Talk" hid the press-to-talk gesture entirely.
                                    else "Hold to talk"
                                }
                                Surface(
                                    color = buttonColor,
                                    shape = RoundedCornerShape(percent = 50),
                                    modifier = Modifier
                                        .fillMaxWidth(if (m.isRound) 0.74f else 0.86f)
                                        .height(m.h(0.21f))
                                        .pointerInput(currentEnrollment) {
                                            detectTapGestures(
                                                onPress = {
                                                    if (!currentEnrollment.canTalk) return@detectTapGestures
                                                    // While Hermes is talking or working, the big button is
                                                    // Stop. The owner must always be able to end speech,
                                                    // however long the reply runs.
                                                    // Only barge in on audio that is really still owed.
                                                    // After a reply finished the flag could linger, so
                                                    // the next press became a silent interrupt and the
                                                    // owner had to press again — press, nothing, press.
                                                    val owed = PocketWearState.expectingReply.value
                                                    val sounding = PocketWearState.speakingAudio.value ||
                                                        PocketWearState.screen.value == "speaking"
                                                    if (owed && sounding) {
                                                        Log.i("HailHermes", "press: interrupt")
                                                        PocketWearState.service?.interrupt()
                                                        return@detectTapGestures
                                                    }
                                                    Log.i("HailHermes", "press: listen")
                                                    if (!hasAudioPermission()) {
                                                        permissions.launch(
                                                            arrayOf(Manifest.permission.RECORD_AUDIO)
                                                        )
                                                        return@detectTapGestures
                                                    }
                                                    val nowPress = System.currentTimeMillis()
                                                    if (!conversationActiveFlow.value) {
                                                        val started = talkController.onGesturePress(nowPress)
                                                        if (started) PocketWearState.service?.buzz("short")
                                                    }
                                                    if (!tryAwaitRelease()) {
                                                        talkController.onGestureCancel()
                                                    } else {
                                                        val nowRelease = System.currentTimeMillis()
                                                        val action = talkController.onTalkGestureRelease(
                                                            nowMs = nowRelease,
                                                            serverScreen = PocketWearState.screen.value,
                                                            conversationActive = conversationActiveFlow.value
                                                        )
                                                        when (action) {
                                                            GestureAction.START_CONVERSATION -> {
                                                                PocketWearState.conversationMode.value = true
                                                                voiceActivity.startConversation(nowRelease)
                                                                conversationActiveFlow.value = true
                                                                PocketWearState.service?.buzz("short")
                                                            }
                                                            GestureAction.STOP_CONVERSATION -> {
                                                                stopConversationMode(softHaptic = true)
                                                            }
                                                            GestureAction.INTERRUPT -> {
                                                                PocketWearState.service?.interrupt()
                                                            }
                                                            else -> {}
                                                        }
                                                    }
                                                }
                                            )
                                        }
                                ) {
                                    Box(contentAlignment = Alignment.Center) {
                                        Text(
                                            text = buttonText,
                                            color = Color.Black,
                                            fontSize = m.talkSp,
                                            fontWeight = FontWeight.Bold,
                                            textAlign = TextAlign.Center,
                                            maxLines = 1,
                                            overflow = TextOverflow.Ellipsis
                                        )
                                    }
                                }
                            }
                        }

                        if (recapNotice != null) {
                            item {
                                Text(
                                    text = recapNotice!!,
                                    fontSize = m.captionSp,
                                    color = Color(0xFF40C4FF),
                                    textAlign = TextAlign.Center,
                                    maxLines = 2,
                                    overflow = TextOverflow.Ellipsis,
                                    modifier = Modifier.fillMaxWidth()
                                )
                            }
                        }

                        if (visibleError != null) {
                            item {
                                Text(
                                    text = visibleError!!,
                                    fontSize = m.captionSp,
                                    color = Color(0xFFFF8A80),
                                    textAlign = TextAlign.Center,
                                    maxLines = 1,
                                    modifier = Modifier.fillMaxWidth().clickable {
                                        PocketWearState.service?.retryConnection()
                                    }
                                )
                            }
                        }
                        val friendlyStatus = when {
                            stillWorking -> "Still working…"
                            workingCount > 0 && state == "idle" && talkState == TalkState.IDLE -> "Hermes is working…"
                            effectiveState == "listening" -> "Listening…"
                            effectiveState == "thinking" -> "Thinking…"
                            effectiveState == "speaking" -> "Speaking…"
                            else -> ""
                        }
                        val statusLine = if (developerMode) {
                            "$status · $effectiveState"
                        } else {
                            friendlyStatus
                        }
                        if (statusLine.isNotEmpty() || busyNow) {
                            // One dot, coloured by what the watch is actually doing: green
                            // while the mic is open, amber while Hermes is thinking, blue
                            // while speech plays. Listening shows a dot even though it is
                            // not "busy", because that is the moment the owner must know the
                            // mic is live.
                            val dotColor = when (effectiveState) {
                                "listening" -> Color(0xFF00C853)
                                "thinking" -> Color(0xFFFFD54F)
                                "speaking" -> Color(0xFF40C4FF)
                                else -> Color(0xFFFFD54F)
                            }
                            val showDot = busyNow || effectiveState == "listening"
                            item {
                                Row(
                                    verticalAlignment = Alignment.CenterVertically,
                                    horizontalArrangement = Arrangement.Center,
                                    modifier = Modifier.fillMaxWidth()
                                ) {
                                    if (showDot) {
                                        PulsingDot(color = dotColor, size = m.h(0.022f))
                                        Spacer(Modifier.width(m.w(0.02f)))
                                    }
                                    Text(
                                        text = statusLine.ifEmpty { "Working…" },
                                        fontSize = m.captionSp,
                                        color = if (effectiveState == "listening" || effectiveState == "thinking" || effectiveState == "speaking")
                                            dotColor else Color(0xFFB0BEC5),
                                        textAlign = TextAlign.Center,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis
                                    )
                                }
                            }
                        }
                        if (developerMode) {
                            item {
                                Text(
                                    text = "${PocketWearState.service?.currentHost() ?: host} · ${deviceId.take(8)}",
                                    fontSize = m.captionSp,
                                    color = Color(0xFF78909C),
                                    textAlign = TextAlign.Center,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis
                                )
                            }
                            if (!lastTurnTiming.isNullOrBlank()) {
                                item {
                                    Text(
                                        text = lastTurnTiming!!,
                                        fontSize = m.captionSp,
                                        color = Color(0xFF80CBC4),
                                        textAlign = TextAlign.Center,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis
                                    )
                                }
                            }
                        }

                        if (volumeOffHint) {
                            item {
                                CompactChip(
                                    onClick = { PocketWearState.service?.raiseMediaVolumeToHalf() },
                                    colors = ChipDefaults.chipColors(backgroundColor = Color(0xFFFFD54F)),
                                    label = {
                                        Text(
                                            text = "Volume is off · Tap 50%",
                                            fontSize = m.captionSp,
                                            color = Color.Black,
                                            maxLines = 1,
                                            overflow = TextOverflow.Ellipsis
                                        )
                                    }
                                )
                            }
                        }

                        if (state == "speaking" || state == "thinking" || busyNow) {
                            item {
                                CompactChip(
                                    onClick = { PocketWearState.service?.interrupt() },
                                    label = { Text("Interrupt", fontSize = m.captionSp) }
                                )
                            }
                        }

                        // Conversation mode was previously reachable only by double-tapping
                        // the talk button, which nobody would guess. It gets a chip.
                        if (currentEnrollment.canTalk && !conversationActive) {
                            item {
                                CompactChip(
                                    onClick = {
                                        if (!hasAudioPermission()) {
                                            permissions.launch(
                                                arrayOf(Manifest.permission.RECORD_AUDIO)
                                            )
                                        } else {
                                            val nowStart = System.currentTimeMillis()
                                            PocketWearState.conversationMode.value = true
                                            voiceActivity.startConversation(nowStart)
                                            conversationActiveFlow.value = true
                                            PocketWearState.service?.buzz("short")
                                        }
                                    },
                                    label = {
                                        Text("Conversation", fontSize = m.captionSp)
                                    }
                                )
                            }
                        }

                        items(transcript) { line ->
                            Surface(
                                color = Color(0xFF1C262B),
                                shape = RoundedCornerShape(m.min(0.05f)),
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Text(
                                    text = line,
                                    fontSize = m.smallSp,
                                    color = Color.White,
                                    modifier = Modifier.padding(
                                        horizontal = m.w(0.05f),
                                        vertical = m.h(0.03f)
                                    )
                                )
                            }
                        }

                        item {
                            CompactChip(
                                onClick = {
                                    PocketWearState.service?.buzz("short")
                                    showSettings = true
                                    PocketWearState.service?.requestWatchSettings()
                                },
                                label = {
                                    Text(
                                        text = "⚙",
                                        fontSize = m.captionSp
                                    )
                                }
                            )
                        }
                    }
                }
            }

            if (isListening || thinking) {
                ListeningEdgeGlowOverlay(isRound = m.isRound, thinking = thinking)
            }
        }
    }
}

@Composable
private fun ListeningEdgeGlowOverlay(isRound: Boolean, thinking: Boolean) {
    val infinite = rememberInfiniteTransition(label = "listeningGlow")
    val pulse by infinite.animateFloat(
        initialValue = 0f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 600, easing = FastOutSlowInEasing),
            repeatMode = RepeatMode.Reverse
        ),
        label = "pulse"
    )
    Canvas(modifier = Modifier.fillMaxSize()) {
        val minSide = minOf(size.width, size.height)
        val outerStroke = minSide * (0.045f + 0.025f * pulse)
        val innerStroke = minSide * (0.022f + 0.012f * pulse)
        val outerAlpha = 0.28f + 0.35f * pulse
        val innerAlpha = 0.65f + 0.35f * pulse
        val glowColor = if (thinking) Color(0xFFFFA000) else Color(0xFF00B0FF)
        val coreColor = if (thinking) Color(0xFFFFD54F) else Color(0xFF80D8FF)

        if (isRound) {
            val radiusOuter = (minSide - outerStroke) / 2f
            val radiusInner = (minSide - innerStroke) / 2f
            drawCircle(
                color = glowColor.copy(alpha = outerAlpha),
                radius = radiusOuter,
                center = center,
                style = Stroke(width = outerStroke)
            )
            drawCircle(
                color = coreColor.copy(alpha = innerAlpha),
                radius = radiusInner,
                center = center,
                style = Stroke(width = innerStroke)
            )
        } else {
            val corner = CornerRadius(minSide * 0.18f, minSide * 0.18f)
            val halfOuter = outerStroke / 2f
            val halfInner = innerStroke / 2f
            drawRoundRect(
                color = glowColor.copy(alpha = outerAlpha),
                topLeft = Offset(halfOuter, halfOuter),
                size = Size(size.width - outerStroke, size.height - outerStroke),
                cornerRadius = corner,
                style = Stroke(width = outerStroke)
            )
            drawRoundRect(
                color = coreColor.copy(alpha = innerAlpha),
                topLeft = Offset(halfInner, halfInner),
                size = Size(size.width - innerStroke, size.height - innerStroke),
                cornerRadius = corner,
                style = Stroke(width = innerStroke)
            )
        }
    }
}

/**
 * A small dot that blinks on a plain delay loop rather than the system animation
 * clock, so it keeps moving when animations are disabled or the device is saving
 * power. While it blinks, Hermes still owes the owner an answer.
 */
@Composable
private fun PulsingDot(color: Color, size: Dp, modifier: Modifier = Modifier) {
    var lit by remember { mutableStateOf(true) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(450)
            lit = !lit
        }
    }
    Canvas(modifier = modifier.size(size)) {
        drawCircle(color = color.copy(alpha = if (lit) 1f else 0.45f))
    }
}
