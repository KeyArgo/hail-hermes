package com.hermes.pocket.wear

import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test

class CodecTest {
    @Test
    fun workingRequiresNonNegativeIntegerCount() {
        fun parse(raw: String) = Codec.decode(raw)?.let(Codec::parseWorking)
        assertEquals(2, parse("""{"type":"working","v":2,"count":2}""")?.count)
        assertEquals(0, parse("""{"type":"working","v":2,"count":0}""")?.count)
        assertNull(parse("""{"type":"working","v":2,"count":-1}"""))
        assertNull(parse("""{"type":"working","v":2,"count":1.5}"""))
        assertNull(parse("""{"type":"working","v":2}"""))
        assertNull(parse("""{"type":"state","v":2,"count":1}"""))
    }

    @Test
    fun helloEncodesWearKindCapabilitiesAndOptionalTokenWithV2() {
        val withToken = Codec.decode(Codec.hello("watch-uuid-1", "tok_abc"))!!
        assertEquals("hello", Codec.field(withToken, "type"))
        assertEquals("2", Codec.field(withToken, "v"))
        assertEquals(2, Codec.intField(withToken, "v"))
        assertEquals("watch-uuid-1", Codec.field(withToken, "device_id"))
        assertEquals("wear", Codec.field(withToken, "kind"))
        assertEquals("tok_abc", Codec.field(withToken, "token"))

        val caps = Codec.objectField(withToken, "caps")!!
        assertTrue(caps["push"]!!.jsonPrimitive.boolean)
        assertTrue(caps["haptic"]!!.jsonPrimitive.boolean)
        assertFalse(caps["led"]!!.jsonPrimitive.boolean)
        assertTrue(caps["battery"]!!.jsonPrimitive.boolean)

        val withoutToken = Codec.decode(Codec.hello("watch-uuid-1", null))!!
        assertEquals(2, Codec.intField(withoutToken, "v"))
        assertFalse(withoutToken.containsKey("token"))
    }

    @Test
    fun allOutgoingClientControlMessagesHaveVersion2AndPairConfirmIsRemoved() {
        val outgoingFrames = listOf(
            Codec.hello("watch-uuid-1", null),
            Codec.hello("watch-uuid-1", "tok_123"),
            Codec.text("hello\n\"world\" ⚡"),
            Codec.audioStart(),
            Codec.audioEnd(),
            Codec.interrupt(),
            Codec.ping(1700000000L)
        )

        for (raw in outgoingFrames) {
            val decoded = Codec.decode(raw)
            assertNotNull("Expected valid v2 frame for: $raw", decoded)
            assertEquals(2, Codec.intField(decoded!!, "v"))
            assertNotEquals("pair_confirm", Codec.field(decoded, "type"))
        }

        val start = Codec.decode(Codec.audioStart())!!
        assertEquals("audio_start", Codec.field(start, "type"))
        assertEquals("pcm16le", Codec.field(start, "codec"))
        assertEquals("16000", Codec.field(start, "rate"))

        val end = Codec.decode(Codec.audioEnd())!!
        assertEquals("audio_end", Codec.field(end, "type"))

        val intr = Codec.decode(Codec.interrupt())!!
        assertEquals("interrupt", Codec.field(intr, "type"))

        val ping = Codec.decode(Codec.ping(1700000000L))!!
        assertEquals("ping", Codec.field(ping, "type"))
        assertEquals("1700000000", Codec.field(ping, "t"))

        val methodNames = Codec::class.java.declaredMethods.map { it.name }
        assertFalse("confirm / pair_confirm must be removed", methodNames.contains("confirm"))
    }

    @Test
    fun roundTripsV2ServerMessages() {
        val pendingRaw = Codec.message(
            "pair_pending",
            buildJsonObject {
                put("request_id", "req_0123456789abcdef")
                put("match_code", "4821")
                put("expires_in", 300)
            }
        )
        val pendingObj = Codec.decode(pendingRaw)!!
        assertEquals(2, Codec.intField(pendingObj, "v"))
        val pending = Codec.parsePairPending(pendingObj)!!
        assertEquals("req_0123456789abcdef", pending.requestId)
        assertEquals("4821", pending.matchCode)
        assertEquals(300, pending.expiresIn)

        val pairedRaw = Codec.message(
            "paired",
            buildJsonObject {
                put("token", "tok_live_secret")
            }
        )
        val pairedObj = Codec.decode(pairedRaw)!!
        assertEquals(2, Codec.intField(pairedObj, "v"))
        val paired = Codec.parsePaired(pairedObj)!!
        assertEquals("tok_live_secret", paired.token)

        val errorRaw = Codec.message(
            "error",
            buildJsonObject {
                put("code", "denied")
                put("message", "Pairing request was denied")
            }
        )
        val errorObj = Codec.decode(errorRaw)!!
        assertEquals(2, Codec.intField(errorObj, "v"))
        val err = Codec.parseError(errorObj)!!
        assertEquals("denied", err.code)
        assertEquals("Pairing request was denied", err.message)

        val hapticRaw = Codec.message(
            "haptic",
            buildJsonObject {
                put("pattern", "double")
            }
        )
        val hapticObj = Codec.decode(hapticRaw)!!
        assertEquals(2, Codec.intField(hapticObj, "v"))
        val haptic = Codec.parseHaptic(hapticObj)!!
        assertEquals("double", haptic.pattern)
    }

    @Test
    fun decodeValidatesVersion2AndIgnoresMalformedOrV1Frames() {
        assertEquals("hello\n\"world\" ⚡", Codec.field(Codec.decode(Codec.text("hello\n\"world\" ⚡"))!!, "text"))
        assertNull(Codec.decode("not json"))
        assertNull(Codec.decode("{\"type\":\"state\",\"v\":1,\"screen\":\"idle\"}"))
        assertNull(Codec.decode("{\"type\":\"state\",\"v\":3,\"screen\":\"idle\"}"))
        assertNull(Codec.decode("{\"v\":2}"))
        assertNull(Codec.decode("{\"type\":\"\",\"v\":2}"))
    }

    @Test
    fun hubMessagesAndScreenNormalization() {
        val stateMsg = Codec.decode("{\"type\":\"state\",\"v\":2,\"screen\":\"listening\"}")!!
        assertEquals("listening", Codec.normalizeScreen(Codec.field(stateMsg, "screen")))
        assertEquals("idle", Codec.normalizeScreen("unknown_state"))
        assertEquals("idle", Codec.normalizeScreen(null))

        val transcript = Codec.decode("{\"type\":\"transcript\",\"v\":2,\"role\":\"assistant\",\"text\":\"Hi watch\",\"final\":true}")!!
        assertEquals("assistant", Codec.field(transcript, "role"))
        assertEquals("Hi watch", Codec.field(transcript, "text"))
        assertEquals(true, Codec.boolField(transcript, "final"))

        val toolEvent = Codec.decode("{\"type\":\"tool_event\",\"v\":2,\"name\":\"device_haptic\",\"args\":{\"pattern\":\"double\"}}")!!
        assertEquals("device_haptic", Codec.field(toolEvent, "name"))
        assertEquals("double", Codec.field(Codec.objectField(toolEvent, "args")!!, "pattern"))
    }

    @Test
    fun pcmChunkingProduces640ByteFrames() {
        val raw = ByteArray(1000) { (it and 0xFF).toByte() }
        val frames = Codec.chunkPcmFrames(raw)
        assertEquals(2, frames.size)
        assertEquals(640, frames[0].size)
        assertEquals(640, frames[1].size)
        assertEquals(0, frames[1][360].toInt())
        assertTrue(Codec.chunkPcmFrames(ByteArray(1)).isEmpty())
    }

    @Test
    fun pairPendingWithDecimalExpiresInIsStillParsed() {
        // The hub once sent expires_in as 299.99; the watch dropped the whole message and never showed the code.
        val obj = Codec.decode("""{"type":"pair_pending","v":2,"request_id":"_7BfMcqudnhLpCauYJdT5w","match_code":"5311","expires_in":299.99989062797977}""")
        val pending = Codec.parsePairPending(obj!!)
        assertEquals("5311", pending!!.matchCode)
        assertEquals(299, pending.expiresIn)
    }
}
