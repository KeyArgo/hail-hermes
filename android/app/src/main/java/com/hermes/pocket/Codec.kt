package com.hermes.pocket

import kotlinx.serialization.json.*

object Codec {
    private val json = Json { ignoreUnknownKeys = true }
    fun message(type: String, fields: JsonObject = buildJsonObject {}): String = buildJsonObject {
        put("type", type); put("v", 1)
        fields.forEach { (key, value) -> if (key != "type" && key != "v") put(key, value) }
    }.toString()
    fun decode(text: String): JsonObject? = try {
        val obj = json.parseToJsonElement(text) as? JsonObject
        if (obj?.get("v")?.jsonPrimitive?.intOrNull == 1 && obj["type"]?.jsonPrimitive?.contentOrNull != null) obj else null
    } catch (_: Exception) { null }
    fun field(obj: JsonObject, key: String): String? = try { obj[key]?.jsonPrimitive?.contentOrNull } catch (_: Exception) { null }
    fun hello(id: String, token: String?) = message("hello", buildJsonObject {
        put("device_id", id); put("name", "Android phone"); put("kind", "phone")
        put("caps", buildJsonObject { put("push", true); put("haptic", false); put("led", false); put("battery", false) })
        if (!token.isNullOrEmpty()) put("token", token)
    })
    fun text(value: String) = message("text", buildJsonObject { put("text", value) })
    fun confirm(code: String) = message("pair_confirm", buildJsonObject { put("code", code) })
    fun audioStart() = message("audio_start", buildJsonObject { put("codec", "pcm16le"); put("rate", 16000) })
}
