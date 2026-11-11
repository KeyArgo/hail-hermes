package com.hermes.pocket

import android.app.*
import android.content.*
import android.content.pm.ServiceInfo
import android.media.*
import android.os.*
import androidx.core.app.NotificationCompat
import androidx.datastore.preferences.core.*
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import kotlinx.serialization.json.*
import okhttp3.*
import okio.ByteString
import okio.ByteString.Companion.toByteString
import java.util.UUID
import java.util.concurrent.TimeUnit

private val Context.store by preferencesDataStore("pocket")
object Pocket {
    val status = MutableStateFlow("Starting")
    val screen = MutableStateFlow("idle")
    val prompt = MutableStateFlow<String?>(null)
    val transcript = MutableStateFlow<List<String>>(emptyList())
    var service: PocketService? = null
}
class PocketService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val client = OkHttpClient.Builder().pingInterval(25, TimeUnit.SECONDS).build()
    private var socket: WebSocket? = null
    private var reconnect: Job? = null
    private var generation = 0
    private var attempt = 0
    private var record: AudioRecord? = null
    private var recording: Job? = null
    private var track: AudioTrack? = null
    private val hostKey = stringPreferencesKey("host")
    private val idKey = stringPreferencesKey("id")
    private val tokenKey = stringPreferencesKey("token")
    private var host = "10.0.2.2:8767"
    private var id = ""
    private var token: String? = null
    override fun onBind(intent: Intent?): IBinder? = null
    override fun onCreate() {
        super.onCreate(); Pocket.service = this
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel("connection", "Connection", NotificationManager.IMPORTANCE_LOW))
        manager.createNotificationChannel(NotificationChannel("messages", "Messages", NotificationManager.IMPORTANCE_DEFAULT))
        val notification = NotificationCompat.Builder(this, "connection").setSmallIcon(android.R.drawable.stat_sys_upload).setContentTitle("Hermes Pocket connected service").setContentIntent(openIntent()).setOngoing(true).build()
        if (Build.VERSION.SDK_INT >= 29) startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC) else startForeground(1, notification)
        scope.launch {
            while (isActive) { delay(25000); socket?.send(Codec.message("ping", buildJsonObject { put("t", System.currentTimeMillis()) })) }
        }
        scope.launch {
            store.edit { if (it[idKey] == null) it[idKey] = UUID.randomUUID().toString() }
            val prefs = store.data.first(); host = prefs[hostKey] ?: host; id = prefs[idKey]!!; token = prefs[tokenKey]
            connect()
        }
    }
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY
    private fun openIntent(): PendingIntent = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    private fun connect() {
        val current = ++generation
        Pocket.status.value = "Connecting"
        val address = if (host.startsWith("ws://") || host.startsWith("wss://")) host.trimEnd('/') else "ws://${host.trimEnd('/')}"
        val url = if (address.endsWith("/pocket")) address else "$address/pocket"
        try {
            socket = client.newWebSocket(Request.Builder().url(url).build(), object : WebSocketListener() {
                override fun onOpen(ws: WebSocket, response: Response) {
                    if (current != generation) { ws.close(1000, "replaced"); return }
                    attempt = 0; Pocket.status.value = "Connected"; ws.send(Codec.hello(id, token))
                }
                override fun onMessage(ws: WebSocket, text: String) { if (current == generation) handle(text) }
                override fun onMessage(ws: WebSocket, bytes: ByteString) { if (current == generation && Pocket.screen.value == "speaking") play(bytes.toByteArray()) }
                override fun onClosing(ws: WebSocket, code: Int, reason: String) { ws.close(code, reason) }
                override fun onClosed(ws: WebSocket, code: Int, reason: String) { lost(current) }
                override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) { lost(current) }
            })
        } catch (e: IllegalArgumentException) { Pocket.status.value = "Invalid hub address"; lost(current) }
    }
    private fun lost(current: Int) {
        if (current != generation) return
        generation++; socket = null; stopTalk(); stopPlayback(); Pocket.screen.value = "idle"; Pocket.status.value = "Disconnected; reconnecting"
        reconnect?.cancel(); val delayMs = (1000L shl attempt.coerceAtMost(5)); attempt++
        reconnect = scope.launch { delay(delayMs); connect() }
    }
    private fun handle(raw: String) {
        val msg = Codec.decode(raw) ?: return
        when (Codec.field(msg, "type")) {
            "welcome" -> { Pocket.status.value = "Ready"; Pocket.prompt.value = null; Pocket.screen.value = Codec.field(msg, "state") ?: "idle" }
            "pair_prompt" -> Pocket.prompt.value = Codec.field(msg, "code")
            "paired" -> Codec.field(msg, "token")?.let { value -> token = value; Pocket.prompt.value = null; scope.launch { store.edit { it[tokenKey] = value } } }
            "state" -> { val state = Codec.field(msg, "screen") ?: return; Pocket.screen.value = state; if (state != "speaking") stopPlayback() }
            "audio_start" -> { if (Pocket.screen.value == "speaking") startPlayback() }
            "audio_end" -> stopPlayback()
            "transcript" -> { val role = Codec.field(msg, "role") ?: ""; val text = Codec.field(msg, "text") ?: return; Pocket.transcript.update { it + "$role: $text" } }
            "notify" -> { val title = Codec.field(msg, "title") ?: "Hermes"; val body = Codec.field(msg, "body") ?: ""; val n = NotificationCompat.Builder(this, "messages").setSmallIcon(android.R.drawable.ic_dialog_info).setContentTitle(title).setContentText(body).setStyle(NotificationCompat.BigTextStyle().bigText(body)).setContentIntent(openIntent()).setAutoCancel(true).build(); try { getSystemService(NotificationManager::class.java).notify((System.currentTimeMillis() and 0x7fffffff).toInt(), n) } catch (_: SecurityException) {} }
            "error" -> Pocket.status.value = "Error: ${Codec.field(msg, "message") ?: "unknown"}"
        }
    }
    fun sendText(text: String) { if (text.isNotBlank() && socket?.send(Codec.text(text)) != true) Pocket.status.value = "Not connected; message not sent" }
    fun confirm(code: String) { socket?.send(Codec.confirm(code)) }
    fun interrupt() { socket?.send(Codec.message("interrupt")) }
    fun settings(newHost: String, resetId: Boolean, forget: Boolean) {
        scope.launch {
            store.edit { p -> p[hostKey] = newHost.trim(); if (resetId) p[idKey] = UUID.randomUUID().toString(); if (forget || resetId || newHost.trim() != host) p.remove(tokenKey) }
            val p = store.data.first(); host = p[hostKey] ?: host; id = p[idKey]!!; token = p[tokenKey]
            reconnect?.cancel(); generation++; socket?.close(1000, "settings changed"); socket = null; stopTalk(); stopPlayback(); connect()
        }
    }
    fun currentHost() = host
    @Synchronized fun startTalk() {
        if (recording != null || socket == null) return
        try {
            val size = maxOf(640, AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT))
            val r = AudioRecord(MediaRecorder.AudioSource.MIC, 16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, size * 2)
            if (r.state != AudioRecord.STATE_INITIALIZED) { r.release(); return }
            r.startRecording(); record = r
            if (socket?.send(Codec.audioStart()) != true) { r.stop(); r.release(); record = null; return }
            recording = scope.launch { val buffer = ByteArray(640); while (isActive) { val n = r.read(buffer, 0, buffer.size); if (n > 0) synchronized(this@PocketService) { if (record === r) socket?.send(buffer.toByteString(0, n)) } else if (n < 0) break } }
        } catch (_: SecurityException) { Pocket.status.value = "Microphone permission required" }
    }
    @Synchronized fun stopTalk() {
        val r = record ?: return
        recording?.cancel(); recording = null; record = null
        try { r.stop() } catch (_: IllegalStateException) {}
        r.release(); socket?.send(Codec.message("audio_end"))
    }
    @Synchronized private fun startPlayback() {
        stopPlayback()
        val size = maxOf(1280, AudioTrack.getMinBufferSize(16000, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT))
        track = AudioTrack.Builder().setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()).setAudioFormat(AudioFormat.Builder().setSampleRate(16000).setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build()).setTransferMode(AudioTrack.MODE_STREAM).setBufferSizeInBytes(size * 2).build().also { it.play() }
    }
    @Synchronized private fun play(data: ByteArray) { if (track == null) startPlayback(); track?.write(data, 0, data.size) }
    @Synchronized private fun stopPlayback() { track?.let { try { it.stop() } catch (_: IllegalStateException) {}; it.release() }; track = null }
    override fun onDestroy() { generation++; reconnect?.cancel(); stopTalk(); stopPlayback(); socket?.close(1000, "service stopped"); scope.cancel(); client.dispatcher.executorService.shutdown(); Pocket.service = null; super.onDestroy() }
}
