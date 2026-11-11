package com.hermes.pocket.wear

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File

class CodecModesTest {
    private fun loadFixture(name: String, fallback: String): String {
        val candidates = listOf(
            File("protocol/fixtures/$name"),
            File("../protocol/fixtures/$name"),
            File("../../protocol/fixtures/$name"),
            File("../../../protocol/fixtures/$name")
        )
        return candidates.firstOrNull { it.isFile }?.readText()?.trim() ?: fallback
    }

    @Test
    fun parseModeFromFixtureAndVariants() {
        val raw = loadFixture(
            "mode.json",
            """{"type":"mode","v":2,"mode":"session","session_id":"s-1","title":"Hermes CLI"}"""
        )
        val obj = Codec.decode(raw)
        assertNotNull(obj)
        val parsed = Codec.parseMode(obj!!)
        assertNotNull(parsed)
        assertEquals(Codec.field(obj, "mode"), parsed!!.mode)
        assertEquals(Codec.field(obj, "session_id"), parsed.sessionId)
        assertEquals(Codec.field(obj, "title"), parsed.title)

        val quickObj = Codec.decode("""{"type":"mode","v":2,"mode":"quick"}""")!!
        val quick = Codec.parseMode(quickObj)!!
        assertEquals("quick", quick.mode)
        assertEquals("Quick ask", quick.displayLabel)
    }

    @Test
    fun parseSessionsFromFixture() {
        val raw = loadFixture(
            "sessions.json",
            """{"type":"sessions","v":2,"items":[{"id":"s-1","title":"Hermes CLI","age":"2m"},{"id":"s-2","title":"Watch UI","age":"1h"}]}"""
        )
        val obj = Codec.decode(raw)
        assertNotNull(obj)
        val parsed = Codec.parseSessions(obj!!)
        assertNotNull(parsed)
        val rawItems = obj["items"] as JsonArray
        assertEquals(rawItems.size, parsed!!.items.size)
        if (rawItems.isNotEmpty()) {
            val firstObj = rawItems[0] as JsonObject
            assertEquals(Codec.field(firstObj, "id"), parsed.items[0].id)
            assertEquals(Codec.field(firstObj, "title"), parsed.items[0].title)
            assertEquals(Codec.field(firstObj, "age") ?: "", parsed.items[0].age)
        }
    }

    @Test
    fun encodeAndDecodeSetModeFixtureAndListSessions() {
        val raw = loadFixture(
            "set_mode.json",
            """{"type":"set_mode","v":2,"mode":"session","session_id":"s-1"}"""
        )
        val fixtureObj = Codec.decode(raw)
        assertNotNull(fixtureObj)
        val fixtureMode = Codec.parseSetMode(fixtureObj!!)
        assertNotNull(fixtureMode)

        val encoded = Codec.setMode(fixtureMode!!.mode, fixtureMode.sessionId)
        val decoded = Codec.decode(encoded)!!
        assertEquals("set_mode", Codec.field(decoded, "type"))
        assertEquals(fixtureMode.mode, Codec.field(decoded, "mode"))
        assertEquals(fixtureMode.sessionId, Codec.field(decoded, "session_id"))

        val listDefault = Codec.decode(Codec.listSessions())!!
        assertEquals("list_sessions", Codec.field(listDefault, "type"))
        assertNull(Codec.intField(listDefault, "limit"))

        val listLimited = Codec.decode(Codec.listSessions(8))!!
        assertEquals(8, Codec.intField(listLimited, "limit"))
    }
}
