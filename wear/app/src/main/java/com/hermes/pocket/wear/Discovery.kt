package com.hermes.pocket.wear

import kotlinx.serialization.json.*

data class Computer(val name: String, val host: String, val port: Int, val fp: String) {
    val address: String get() = if (host.contains(':')) "[$host]:$port" else "$host:$port"
}

object Discovery {
    fun parse(json: String, sender: String): Computer? = try {
        val obj = Json.parseToJsonElement(json).jsonObject
        val service = obj["svc"]?.jsonPrimitive
        val version = obj["v"]?.jsonPrimitive
        val name = obj["name"]?.jsonPrimitive
        val fingerprint = obj["fp"]?.jsonPrimitive
        val portValue = obj["port"]?.jsonPrimitive
        val port = portValue?.intOrNull
        if (service?.isString != true || service.content != "hail-hermes" ||
            version?.isString != false || version.intOrNull != 1 ||
            name?.isString != true || name.content.isBlank() || name.content.length > 128 ||
            fingerprint?.isString != true || fingerprint.content.isBlank() || fingerprint.content.length > 256 ||
            portValue?.isString != false || port == null || port !in 1..65535 || sender.isBlank()) null
        else Computer(name.content.trim(), sender, port, fingerprint.content.trim())
    } catch (_: Exception) { null }

    fun preferred(computers: List<Computer>, savedFp: String?): Computer? {
        savedFp?.takeIf { it.isNotBlank() }?.let { fp ->
            return computers.firstOrNull { it.fp == fp }
        }
        // No remembered fingerprint (a reinstall, or a pairing that never completed) used to
        // mean the watch searched forever while the owner stared at a gear icon. One computer
        // on this network is unambiguous; the token still has to match before anything opens.
        return computers.singleOrNull()
    }
}

sealed class SearchState {
    data object Idle : SearchState()
    data object Looking : SearchState()
    data class Choices(val computers: List<Computer>) : SearchState()
    data object NotFound : SearchState()
}

class SearchMachine {
    var state: SearchState = SearchState.Idle
        private set
    private var startedAt = 0L
    fun start(now: Long) { startedAt = now; state = SearchState.Looking }
    fun stop() { state = SearchState.Idle }
    fun fail() { if (state is SearchState.Looking) state = SearchState.NotFound }
    fun choices(computers: List<Computer>) { if (state is SearchState.Looking || state is SearchState.Choices) state = SearchState.Choices(computers) }
    fun tick(now: Long) {
        if ((state is SearchState.Looking || state is SearchState.Choices) && now - startedAt >= TIMEOUT_MS) {
            state = if (state is SearchState.Choices) state else SearchState.NotFound
        }
    }
    companion object { const val TIMEOUT_MS = 30_000L }
}
