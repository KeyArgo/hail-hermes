package com.hermes.pocket.wear

import android.content.Context
import android.graphics.BitmapFactory
import androidx.compose.animation.core.*
import androidx.compose.foundation.Canvas
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import kotlinx.coroutines.delay
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.sin

class MascotAssets(
    val idle1: ImageBitmap? = null,
    val idle2: ImageBitmap? = null,
    val blink: ImageBitmap? = null,
    val talkFrames: List<ImageBitmap> = emptyList()
) {
    val hasIdleAssets: Boolean get() = idle1 != null || blink != null
    // One distinct frame is a still image: keep the moving speaking bars instead,
    // otherwise "speaking" looks exactly like idle (the Nous pack shipped 4 copies).
    val hasTalkAssets: Boolean get() = talkFrames.size >= 2

    fun faceFrame(isBlinking: Boolean, idleAlt: Boolean): ImageBitmap? = when {
        isBlinking && blink != null -> blink
        idleAlt && idle2 != null -> idle2
        else -> idle1 ?: idle2 ?: blink
    }

    companion object {
        private const val PREFS_NAME = "pocket_wear"
        private const val KEY_CHARACTER = "character"

        fun loadSavedCharacter(context: Context): String {
            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            return Codec.normalizeCharacter(prefs.getString(KEY_CHARACTER, Codec.CHARACTER_HERMES))
        }

        fun saveCharacter(context: Context, character: String) {
            context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit()
                .putString(KEY_CHARACTER, Codec.normalizeCharacter(character))
                .apply()
        }

        fun load(context: Context, character: String = Codec.CHARACTER_HERMES): MascotAssets {
            val assetDir = Codec.characterAssetDir(character)
            fun loadBytes(name: String): ByteArray? = try {
                context.assets.open("$assetDir/$name").use { it.readBytes() }
            } catch (_: Exception) {
                null
            }

            fun loadOne(name: String): ImageBitmap? =
                loadBytes(name)?.let { bytes ->
                    BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap()
                }

            val idle1 = loadOne("idle_1.png")
            val idle2 = loadOne("idle_2.png")
            val blink = loadOne("blink.png")
            // Some character packs ship talk frames that are all the same image (the Nous
            // pack does). Kept as-is they report hasTalkAssets=true and animate nothing,
            // which also suppresses the speaking bars. Drop duplicates so the bars run.
            val talks = (1..4)
                .mapNotNull { loadBytes("talk_$it.png") }
                .distinctBy { it.size to it.contentHashCode() }
                .mapNotNull { bytes -> BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap() }
            return MascotAssets(idle1, idle2, blink, talks)
        }
    }
}

@Composable
fun MascotCanvas(
    state: String,
    modifier: Modifier = Modifier,
    character: String? = null
) {
    val context = LocalContext.current
    val hubCharacter by Codec.characterFlow.collectAsState()
    val savedCharacter = remember(context) {
        MascotAssets.loadSavedCharacter(context).also { Codec.restoreSavedCharacter(it) }
    }
    val activeCharacter = Codec.normalizeCharacter(character ?: hubCharacter ?: savedCharacter)
    LaunchedEffect(activeCharacter) {
        MascotAssets.saveCharacter(context, activeCharacter)
    }
    val assets = remember(context, activeCharacter) {
        MascotAssets.load(context, activeCharacter)
    }

    val transition = rememberInfiniteTransition(label = "mascot")
    val phase by transition.animateFloat(
        initialValue = 0f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 1200, easing = LinearEasing),
            repeatMode = RepeatMode.Restart
        ),
        label = "phase"
    )

    var isBlinking by remember { mutableStateOf(false) }
    var idleAlt by remember { mutableStateOf(false) }
    val normState = Codec.normalizeScreen(state)
    // Drive her mouth from actual playback, not from the hub's label: the screen state can
    // read idle while audio is still coming out (and vice versa), which showed a still face
    // during real speech. speakingAudio is set on audio_start and cleared when the stream
    // drains, so this is true only while sound is really playing.
    val speakingAudio by PocketWearState.speakingAudio.collectAsState()
    val talking = speakingAudio
    // A timer, not an animation: Compose's infinite transition did not advance on this
    // device, leaving the face stuck on the first talk frame for the whole sentence.
    var talkIdx by remember { mutableStateOf(0) }
    LaunchedEffect(talking) {
        if (!talking) {
            talkIdx = 0
            return@LaunchedEffect
        }
        while (true) {
            delay(140)
            val n = assets.talkFrames.size
            if (n > 0) talkIdx = (talkIdx + 1) % n
        }
    }
    LaunchedEffect(normState) {
        if (normState != "speaking") {
            while (true) {
                delay(2600)
                isBlinking = true
                delay(160)
                isBlinking = false
                idleAlt = !idleAlt
            }
        } else {
            isBlinking = false
        }
    }

    Canvas(modifier = modifier) {
        val restingBmp = assets.faceFrame(isBlinking, idleAlt)
        // Real playback wins over the hub's screen label: audio can be coming out while the
        // hub has already flipped the screen to idle, and then a label-driven face stands
        // still through the whole sentence.
        val visualState = if (talking) "speaking" else normState
        when (visualState) {
            "idle" -> {
                if (restingBmp != null) {
                    drawAssetBitmap(restingBmp)
                } else {
                    drawProceduralIdle(isBlinking, phase)
                }
            }
            "speaking" -> {
                if (assets.hasTalkAssets && talking) {
                    val idx = talkIdx % assets.talkFrames.size
                    drawAssetBitmap(assets.talkFrames[idx])
                } else if (restingBmp != null) {
                    drawAssetBitmap(restingBmp)
                    drawSpeakingBars(phase, yFactor = 0.86f)
                } else {
                    drawProceduralSpeaking(phase)
                }
            }
            "listening" -> {
                if (restingBmp != null) {
                    drawListeningRings(phase)
                    drawAssetBitmap(restingBmp)
                } else {
                    drawProceduralListening(isBlinking, phase)
                }
            }
            "thinking" -> {
                if (restingBmp != null) {
                    drawAssetBitmap(restingBmp)
                    drawThinkingDots(phase, baseYFactor = 0.86f)
                } else {
                    drawProceduralThinking(isBlinking, phase)
                }
            }
        }
    }
}

private fun DrawScope.drawAssetBitmap(bitmap: ImageBitmap) {
    val dstSize = minOf(size.width, size.height).toInt()
    val dx = ((size.width - dstSize) / 2f).toInt()
    val dy = ((size.height - dstSize) / 2f).toInt()
    drawImage(
        image = bitmap,
        dstOffset = IntOffset(dx, dy),
        dstSize = IntSize(dstSize, dstSize)
    )
}

private fun DrawScope.drawVisorOutline(accent: Color) {
    val w = size.width
    val h = size.height
    val visorW = w * 0.78f
    val visorH = h * 0.64f
    val left = (w - visorW) / 2f
    val top = (h - visorH) / 2f
    drawRoundRect(
        color = accent.copy(alpha = 0.22f),
        topLeft = Offset(left, top),
        size = Size(visorW, visorH),
        cornerRadius = CornerRadius(visorH * 0.35f, visorH * 0.35f),
        style = Stroke(width = w * 0.025f)
    )
}

private fun DrawScope.drawEyes(blink: Boolean, color: Color, yOffsetFactor: Float = 0.42f) {
    val w = size.width
    val h = size.height
    val eyeY = h * yOffsetFactor
    val leftX = w * 0.35f
    val rightX = w * 0.65f
    val eyeW = w * 0.13f
    val eyeH = if (blink) h * 0.03f else h * 0.16f

    drawRoundRect(
        color = color,
        topLeft = Offset(leftX - eyeW / 2f, eyeY - eyeH / 2f),
        size = Size(eyeW, eyeH),
        cornerRadius = CornerRadius(eyeW * 0.45f, eyeW * 0.45f)
    )
    drawRoundRect(
        color = color,
        topLeft = Offset(rightX - eyeW / 2f, eyeY - eyeH / 2f),
        size = Size(eyeW, eyeH),
        cornerRadius = CornerRadius(eyeW * 0.45f, eyeW * 0.45f)
    )
}

private fun DrawScope.drawProceduralIdle(blink: Boolean, phase: Float) {
    val cyan = Color(0xFF4DD0E1)
    drawVisorOutline(cyan)
    val bob = sin(phase * 2f * PI.toFloat()) * (size.height * 0.015f)
    val w = size.width
    val h = size.height
    drawEyes(blink = blink, color = cyan, yOffsetFactor = 0.40f + bob / h)

    // Friendly neutral smile line
    val mouthW = w * 0.22f
    val mouthH = h * 0.035f
    val mouthY = h * 0.62f + bob
    drawRoundRect(
        color = cyan,
        topLeft = Offset((w - mouthW) / 2f, mouthY),
        size = Size(mouthW, mouthH),
        cornerRadius = CornerRadius(mouthH / 2f, mouthH / 2f)
    )
}

private fun DrawScope.drawListeningRings(phase: Float) {
    val green = Color(0xFF69F0AE)
    val center = Offset(size.width / 2f, size.height / 2f)
    val maxR = minOf(size.width, size.height) * 0.48f

    for (i in 0 until 3) {
        val ringPhase = (phase + i / 3f) % 1f
        val radius = maxR * (0.25f + 0.75f * ringPhase)
        val alpha = (1f - ringPhase).coerceIn(0.1f, 0.9f)
        drawCircle(
            color = green.copy(alpha = alpha),
            radius = radius,
            center = center,
            style = Stroke(width = minOf(size.width, size.height) * 0.03f)
        )
    }
}

private fun DrawScope.drawProceduralListening(blink: Boolean, phase: Float) {
    val green = Color(0xFF69F0AE)
    drawVisorOutline(green)
    drawListeningRings(phase)
    drawEyes(blink = blink, color = green, yOffsetFactor = 0.45f)
}

private fun DrawScope.drawThinkingDots(phase: Float, baseYFactor: Float) {
    val amber = Color(0xFFFFD54F)
    val w = size.width
    val h = size.height
    val baseY = h * baseYFactor
    val spacing = w * 0.14f
    for (i in -1..1) {
        val dotPhase = (phase * 2f * PI.toFloat()) - (i + 1) * 0.85f
        val dy = sin(dotPhase) * (h * 0.055f)
        val radius = minOf(w, h) * (0.042f + 0.012f * abs(sin(dotPhase)))
        drawCircle(
            color = amber,
            radius = radius,
            center = Offset(w / 2f + i * spacing, baseY + dy)
        )
    }
}

private fun DrawScope.drawProceduralThinking(blink: Boolean, phase: Float) {
    val amber = Color(0xFFFFD54F)
    drawVisorOutline(amber)
    drawEyes(blink = blink, color = amber.copy(alpha = 0.75f), yOffsetFactor = 0.36f)
    drawThinkingDots(phase, baseYFactor = 0.64f)
}

private fun DrawScope.drawSpeakingBars(phase: Float, yFactor: Float) {
    val sky = Color(0xFF40C4FF)
    val w = size.width
    val h = size.height
    val mouthCenterY = h * yFactor
    val barCount = 5
    val barW = w * 0.055f
    val gap = w * 0.03f
    val totalW = barCount * barW + (barCount - 1) * gap
    val startX = (w - totalW) / 2f

    for (i in 0 until barCount) {
        val wave = abs(sin((phase * 4f * PI.toFloat()) + i * 0.7f))
        val barH = h * (0.05f + 0.16f * wave)
        drawRoundRect(
            color = sky,
            topLeft = Offset(startX + i * (barW + gap), mouthCenterY - barH / 2f),
            size = Size(barW, barH),
            cornerRadius = CornerRadius(barW / 2f, barW / 2f)
        )
    }
}

private fun DrawScope.drawProceduralSpeaking(phase: Float) {
    val sky = Color(0xFF40C4FF)
    drawVisorOutline(sky)
    drawEyes(blink = false, color = sky, yOffsetFactor = 0.36f)
    drawSpeakingBars(phase, yFactor = 0.64f)
}
