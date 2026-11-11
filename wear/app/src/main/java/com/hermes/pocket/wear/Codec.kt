package com.hermes.pocket.wear

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.serialization.json.*

object Codec {
    const val PROTOCOL_VERSION = 2
    const val CODEC_PCM16LE = "pcm16le"
    const val SAMPLE_RATE = 16000
    const val FRAME_BYTES = 640
    val VALID_SCREENS = setOf("idle", "listening", "thinking", "speaking")
    const val MODE_QUICK = "quick"
    const val MODE_SESSION = "session"
    val VALID_MODES = setOf(MODE_QUICK, MODE_SESSION)
    const val CHARACTER_HERMES = "hermes"
    const val CHARACTER_NOUS = "nous"
    const val CHARACTER_NOUS_LIGHT = "nous-light"
    const val CHARACTER_NOUS_DARK = "nous-dark"
    const val CHARACTER_NOUS_WHITE = "nous-white"
    const val CHARACTER_NOUS_SOFT = "nous-soft"
    const val CHARACTER_NOUS_TEAL = "nous-teal"
    const val CHARACTER_NOUS_WARM = "nous-warm"
    val VALID_CHARACTERS = setOf(
        CHARACTER_HERMES, CHARACTER_NOUS, CHARACTER_NOUS_LIGHT, CHARACTER_NOUS_DARK,
        CHARACTER_NOUS_WHITE, CHARACTER_NOUS_SOFT, CHARACTER_NOUS_TEAL, CHARACTER_NOUS_WARM
    )
    private val _characterFlow = MutableStateFlow<String?>(null)
    val characterFlow: StateFlow<String?> = _characterFlow

    private val json = Json { ignoreUnknownKeys = true }

    data class PairPending(
        val requestId: String,
        val matchCode: String,
        val expiresIn: Int
    )

    data class Paired(
        val token: String
    )

    data class ErrorMessage(
        val code: String,
        val message: String
    )

    data class WorkingMessage(val count: Int)

    data class HapticMessage(
        val pattern: String
    )

    data class ModeMessage(
        val mode: String,
        val sessionId: String? = null,
        val title: String? = null
    ) {
        val displayLabel: String
            get() = if (mode == MODE_SESSION) {
                title?.takeIf { it.isNotBlank() } ?: sessionId ?: "Session"
            } else "Quick ask"
    }

    data class SessionItem(
        val id: String,
        val title: String,
        val age: String,
        val folder: String = ""
    )

    data class SessionsMessage(
        val items: List<SessionItem>
    ) : List<SessionItem> by items

    data class SlotItem(
        val slot: Int,
        val id: String,
        val title: String
    )

    data class SlotsMessage(
        val items: List<SlotItem>
    ) : List<SlotItem> by items

    data class StateMessage(
        val screen: String,
        val conversationEnd: Boolean = false,
        val timingText: String? = null
    )

    data class CharacterMessage(
        val id: String
    ) {
        val character: String get() = id
        val assetDir: String get() = characterAssetDir(id)
    }

    fun message(type: String, fields: JsonObject = buildJsonObject {}): String = buildJsonObject {
        put("type", type)
        put("v", PROTOCOL_VERSION)
        fields.forEach { (key, value) ->
            if (key != "type" && key != "v") {
                put(key, value)
            }
        }
    }.toString()

    fun decode(text: String): JsonObject? = try {
        val obj = json.parseToJsonElement(text) as? JsonObject
        val v = obj?.get("v")?.jsonPrimitive?.intOrNull
        val t = obj?.get("type")?.jsonPrimitive?.contentOrNull
        if (v == PROTOCOL_VERSION && !t.isNullOrEmpty()) {
            parseCharacter(obj)?.let { _characterFlow.value = it.id }
            obj
        } else null
    } catch (_: Exception) {
        null
    }

    fun field(obj: JsonObject, key: String): String? = try {
        val el = obj[key]
        if (el is JsonNull) null else el?.jsonPrimitive?.contentOrNull
    } catch (_: Exception) {
        null
    }

    // Accept 300 and 299.99 alike: a whole-number field sent as a decimal must not drop the whole message.
    fun intField(obj: JsonObject, key: String): Int? = try {
        val p = obj[key]?.jsonPrimitive
        p?.intOrNull ?: p?.doubleOrNull?.toInt()
    } catch (_: Exception) {
        null
    }

    fun boolField(obj: JsonObject, key: String): Boolean? = try {
        obj[key]?.jsonPrimitive?.booleanOrNull
    } catch (_: Exception) {
        null
    }

    fun objectField(obj: JsonObject, key: String): JsonObject? = try {
        obj[key] as? JsonObject
    } catch (_: Exception) {
        null
    }

    fun parsePairPending(obj: JsonObject): PairPending? {
        if (field(obj, "type") != "pair_pending") return null
        val requestId = field(obj, "request_id") ?: return null
        val matchCode = field(obj, "match_code") ?: return null
        val expiresIn = intField(obj, "expires_in") ?: return null
        if (requestId.isEmpty() || matchCode.isEmpty()) return null
        return PairPending(
            requestId = requestId,
            matchCode = matchCode,
            expiresIn = expiresIn
        )
    }

    fun parsePaired(obj: JsonObject): Paired? {
        if (field(obj, "type") != "paired") return null
        val token = field(obj, "token") ?: return null
        if (token.isEmpty()) return null
        return Paired(token = token)
    }

    fun parseError(obj: JsonObject): ErrorMessage? {
        if (field(obj, "type") != "error") return null
        val code = field(obj, "code") ?: return null
        if (code.isEmpty()) return null
        val message = field(obj, "message") ?: ""
        return ErrorMessage(code = code, message = message)
    }

    fun parseWorking(obj: JsonObject): WorkingMessage? {
        if (field(obj, "type") != "working") return null
        val count = try { obj["count"]?.jsonPrimitive?.intOrNull } catch (_: Exception) { null }
        if (count == null || count < 0) return null
        return WorkingMessage(count)
    }

    fun parseHaptic(obj: JsonObject): HapticMessage? {
        if (field(obj, "type") != "haptic") return null
        val pattern = field(obj, "pattern")?.takeIf { it.isNotEmpty() } ?: "short"
        return HapticMessage(pattern = pattern)
    }

    fun parseMode(obj: JsonObject): ModeMessage? {
        if (field(obj, "type") != "mode") return null
        val mode = field(obj, "mode")?.takeIf { it in VALID_MODES } ?: return null
        val sessionId = field(obj, "session_id")?.takeIf { it.isNotEmpty() }
        if (mode == MODE_SESSION && sessionId == null) return null
        val title = field(obj, "title")?.takeIf { it.isNotEmpty() }
        return ModeMessage(mode = mode, sessionId = sessionId, title = title)
    }

    fun parseSetMode(obj: JsonObject): ModeMessage? {
        if (field(obj, "type") != "set_mode") return null
        val mode = field(obj, "mode")?.takeIf { it in VALID_MODES } ?: return null
        val sessionId = field(obj, "session_id")?.takeIf { it.isNotEmpty() }
        if (mode == MODE_SESSION && sessionId == null) return null
        return ModeMessage(mode = mode, sessionId = sessionId)
    }

    fun parseSessions(obj: JsonObject): SessionsMessage? {
        if (field(obj, "type") != "sessions") return null
        val arr = (obj["items"] as? JsonArray) ?: return null
        val items = arr.mapNotNull { el ->
            val itemObj = el as? JsonObject ?: return@mapNotNull null
            val id = field(itemObj, "id")?.takeIf { it.isNotEmpty() } ?: return@mapNotNull null
            val title = field(itemObj, "title")?.takeIf { it.isNotEmpty() } ?: id
            val age = field(itemObj, "age") ?: ""
            val folder = field(itemObj, "folder") ?: ""
            SessionItem(id = id, title = title, age = age, folder = folder)
        }
        return SessionsMessage(items = items)
    }

    fun parseSlots(obj: JsonObject): SlotsMessage? {
        if (field(obj, "type") != "slots") return null
        val arr = (obj["items"] as? JsonArray) ?: return null
        val items = arr.mapNotNull { el ->
            val itemObj = el as? JsonObject ?: return@mapNotNull null
            val slot = intField(itemObj, "slot") ?: return@mapNotNull null
            if (slot !in 1..3) return@mapNotNull null
            val id = field(itemObj, "id")?.takeIf { it.isNotEmpty() } ?: return@mapNotNull null
            val title = field(itemObj, "title")?.takeIf { it.isNotEmpty() } ?: id
            SlotItem(slot = slot, id = id, title = title)
        }.sortedBy { it.slot }.distinctBy { it.slot }.take(3)
        return SlotsMessage(items = items)
    }

    fun parseState(obj: JsonObject): StateMessage? {
        if (field(obj, "type") != "state") return null
        val screen = normalizeScreen(field(obj, "screen"))
        val conversationEnd = boolField(obj, "conversation_end") ?: false
        val timingText = extractTimingSummary(obj)
        return StateMessage(
            screen = screen,
            conversationEnd = conversationEnd,
            timingText = timingText
        )
    }

    fun parseCharacter(obj: JsonObject): CharacterMessage? {
        if (field(obj, "type") != "character") return null
        val id = (field(obj, "id") ?: field(obj, "character"))
            ?.trim()
            ?.lowercase()
            ?.takeIf { it in VALID_CHARACTERS }
            ?: return null
        _characterFlow.value = id
        return CharacterMessage(id = id)
    }

    fun extractTimingSummary(obj: JsonObject): String? {
        field(obj, "last_turn_timing")?.takeIf { it.isNotBlank() }?.let { return it }
        field(obj, "turn_timing")?.takeIf { it.isNotBlank() }?.let { return it }
        val timingObj = objectField(obj, "timing") ?: objectField(obj, "timings") ?: obj
        val stt = intField(timingObj, "stt_ms")
        val llm = intField(timingObj, "llm_ms")
        val tts = intField(timingObj, "tts_ms")
        val total = intField(timingObj, "total_ms") ?: intField(timingObj, "turn_ms") ?: intField(timingObj, "ms")
        val parts = mutableListOf<String>()
        if (stt != null) parts.add("STT ${stt}ms")
        if (llm != null) parts.add("LLM ${llm}ms")
        if (tts != null) parts.add("TTS ${tts}ms")
        if (total != null) parts.add("Total ${total}ms")
        return if (parts.isNotEmpty()) parts.joinToString(" · ") else null
    }

    fun cycleSlotMode(
        current: ModeMessage,
        slots: List<SlotItem>,
        direction: Int
    ): ModeMessage {
        val modes = buildList {
            add(ModeMessage(mode = MODE_QUICK))
            slots.take(3).forEach { slot ->
                add(ModeMessage(mode = MODE_SESSION, sessionId = slot.id, title = slot.title))
            }
        }
        if (modes.size <= 1 || direction == 0) return modes.first()
        val currentIndex = modes.indexOfFirst {
            if (it.mode == MODE_QUICK) current.mode == MODE_QUICK
            else current.mode == MODE_SESSION && current.sessionId == it.sessionId
        }.let { if (it < 0) 0 else it }
        val step = if (direction > 0) 1 else -1
        val nextIndex = (currentIndex + step).mod(modes.size)
        return modes[nextIndex]
    }

    fun hello(
        id: String,
        token: String?,
        name: String = "WearOS Watch",
        push: Boolean = true,
        haptic: Boolean = true,
        led: Boolean = false,
        battery: Boolean = true
    ): String = message("hello", buildJsonObject {
        put("modes", true)
        put("device_id", id)
        put("name", name)
        put("kind", "wear")
        put("caps", buildJsonObject {
            put("push", push)
            put("haptic", haptic)
            put("led", led)
            put("battery", battery)
        })
        if (!token.isNullOrEmpty()) {
            put("token", token)
        }
    })

    fun text(value: String): String = message("text", buildJsonObject {
        put("text", value)
    })

    fun audioStart(): String = message("audio_start", buildJsonObject {
        put("codec", CODEC_PCM16LE)
        put("rate", SAMPLE_RATE)
    })

    fun audioEnd(): String = message("audio_end")

    fun interrupt(): String = message("interrupt")

    fun ping(t: Long): String = message("ping", buildJsonObject {
        put("t", t)
    })

    fun setMode(mode: String, sessionId: String? = null): String {
        require(mode in VALID_MODES) { "Invalid mode: $mode" }
        require(mode != MODE_SESSION || !sessionId.isNullOrBlank()) { "session_id required" }
        return message("set_mode", buildJsonObject {
            put("mode", mode)
            if (mode == MODE_SESSION && !sessionId.isNullOrBlank()) {
                put("session_id", sessionId)
            }
        })
    }

    fun getSettings(req: String = "s1"): String = message("get_settings", buildJsonObject {
        put("req", req)
    })

    fun listModels(): String = message("list_models", buildJsonObject { })

    fun setSetting(key: String, value: String, req: String = "s2"): String =
        message("set_setting", buildJsonObject {
            put("key", key)
            put("value", value)
            put("req", req)
        })

    fun parseModelItems(obj: JsonObject): List<Pair<String, String>> {
        val arr = obj["items"] as? JsonArray ?: return emptyList()
        return arr.mapNotNull { el ->
            val item = el as? JsonObject ?: return@mapNotNull null
            val source = field(item, "source") ?: return@mapNotNull null
            val id = field(item, "id").orEmpty()
            val label = field(item, "label") ?: "$source $id"
            ("$source::$id") to label
        }
    }

    fun recap(sessionId: String): String = message("recap", buildJsonObject {
        put("session_id", sessionId)
    })

    fun listSessions(limit: Int? = null): String = message("list_sessions", buildJsonObject {
        if (limit != null) {
            put("limit", limit.coerceIn(1, 30))
        }
    })

    fun normalizeScreen(screen: String?): String =
        if (screen != null && screen in VALID_SCREENS) screen else "idle"

    fun normalizeCharacter(character: String?): String {
        val norm = character?.trim()?.lowercase()
        return if (norm != null && norm in VALID_CHARACTERS) norm else CHARACTER_HERMES
    }

    fun characterAssetDir(character: String?): String = when (normalizeCharacter(character)) {
        CHARACTER_NOUS_WHITE -> "mascot-nous-white"
        CHARACTER_NOUS_SOFT -> "mascot-nous-soft"
        CHARACTER_NOUS_TEAL -> "mascot-nous-teal"
        CHARACTER_NOUS_WARM -> "mascot-nous-warm"
        CHARACTER_NOUS_LIGHT -> "mascot-nous-light"
        CHARACTER_NOUS_DARK -> "mascot-nous-dark"
        CHARACTER_NOUS -> "mascot-nous"
        else -> "mascot"
    }

    fun restoreSavedCharacter(character: String?) {
        _characterFlow.compareAndSet(null, normalizeCharacter(character))
    }

    fun character(id: String): String {
        require(id.trim().lowercase() in VALID_CHARACTERS) { "Invalid character: $id" }
        return message("character", buildJsonObject {
            put("id", id.trim().lowercase())
        })
    }

    fun chunkPcmFrames(pcm: ByteArray, frameBytes: Int = FRAME_BYTES): List<ByteArray> {
        val evenLen = pcm.size and 1.inv()
        if (evenLen <= 0) return emptyList()
        val frames = mutableListOf<ByteArray>()
        var offset = 0
        while (offset < evenLen) {
            val chunk = ByteArray(frameBytes)
            val toCopy = minOf(frameBytes, evenLen - offset)
            System.arraycopy(pcm, offset, chunk, 0, toCopy)
            frames.add(chunk)
            offset += frameBytes
        }
        return frames
    }
}
