package com.hermes.pocket

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat

class MainActivity : ComponentActivity() {
    private val permissions = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { }
    private var shared by mutableStateOf<String?>(null)
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ContextCompat.startForegroundService(this, Intent(this, PocketService::class.java))
        val requested = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 33) requested.add(Manifest.permission.POST_NOTIFICATIONS)
        permissions.launch(requested.toTypedArray()); receive(intent)
        setContent { MaterialTheme { App() } }
    }
    override fun onNewIntent(intent: Intent) { super.onNewIntent(intent); setIntent(intent); receive(intent) }
    private fun receive(intent: Intent?) {
        if (intent?.action != Intent.ACTION_SEND) return
        shared = intent.getStringExtra(Intent.EXTRA_TEXT) ?: intent.getParcelableExtra<android.net.Uri>(Intent.EXTRA_STREAM)?.toString()
    }
    @Composable fun App() {
        val status by Pocket.status.collectAsState(); val state by Pocket.screen.collectAsState()
        val prompt by Pocket.prompt.collectAsState(); val lines by Pocket.transcript.collectAsState()
        var settings by remember { mutableStateOf(false) }; var input by remember { mutableStateOf("") }
        var host by remember(settings) { mutableStateOf(Pocket.service?.currentHost() ?: "10.0.2.2:8767") }
        LaunchedEffect(shared, status) { if (shared != null && status == "Ready") { Pocket.service?.sendText(shared!!); shared = null } }
        Column(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Hermes Pocket", style = MaterialTheme.typography.headlineSmall)
            Text("$status · $state")
            if (settings) {
                OutlinedTextField(host, { host = it }, label = { Text("Hub host:port or ws URL") }, modifier = Modifier.fillMaxWidth())
                Button(onClick = { Pocket.service?.settings(host, false, false); settings = false }) { Text("Save and reconnect") }
                Button(onClick = { Pocket.service?.settings(host, false, true); settings = false }) { Text("Forget pairing") }
                Button(onClick = { Pocket.service?.settings(host, true, true); settings = false }) { Text("Reset device ID") }
                TextButton(onClick = { settings = false }) { Text("Back") }
            } else {
                TextButton(onClick = { settings = true }) { Text("Debug settings") }
                prompt?.let { code -> Text("Check the hub code: $code"); Button(onClick = { Pocket.service?.confirm(code) }) { Text("Confirm pairing") } }
                LazyColumn(Modifier.weight(1f).fillMaxWidth()) { items(lines) { Text(it, modifier = Modifier.padding(vertical = 4.dp)) } }
                if (state == "speaking") Button(onClick = { Pocket.service?.interrupt() }) { Text("Interrupt") }
                Surface(color = MaterialTheme.colorScheme.primary, shape = MaterialTheme.shapes.extraLarge, modifier = Modifier.fillMaxWidth().height(90.dp).pointerInput(Unit) {
                    detectTapGestures(onPress = {
                        if (ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                            Pocket.service?.startTalk(); try { awaitRelease() } finally { Pocket.service?.stopTalk() }
                        } else { permissions.launch(arrayOf(Manifest.permission.RECORD_AUDIO)) }
                    })
                }) { Box(contentAlignment = androidx.compose.ui.Alignment.Center) { Text("Hold to talk", color = MaterialTheme.colorScheme.onPrimary) } }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(input, { input = it }, label = { Text("Message") }, modifier = Modifier.weight(1f))
                    Button(onClick = { Pocket.service?.sendText(input); input = "" }) { Text("Send") }
                }
            }
        }
    }
}
