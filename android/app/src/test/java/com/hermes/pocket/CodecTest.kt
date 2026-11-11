package com.hermes.pocket

import org.junit.Assert.*
import org.junit.Test
import kotlinx.serialization.json.*

class CodecTest {
    @Test fun helloContainsCapabilitiesAndOptionalToken() {
        val hello = Codec.decode(Codec.hello("device", "secret"))!!
        assertEquals("hello", Codec.field(hello, "type")); assertEquals("secret", Codec.field(hello, "token"))
        assertEquals("phone", Codec.field(hello, "kind")); assertEquals(true, hello["caps"]!!.jsonObject["push"]!!.jsonPrimitive.boolean)
        assertFalse(Codec.decode(Codec.hello("device", null))!!.containsKey("token"))
    }
    @Test fun escapingAndVersionValidation() {
        assertEquals("a\"\n☃", Codec.field(Codec.decode(Codec.text("a\"\n☃"))!!, "text"))
        assertNull(Codec.decode("broken")); assertNull(Codec.decode("{\"type\":\"state\",\"v\":2}"))
        assertNull(Codec.decode("{\"v\":1}"))
    }
    @Test fun audioAndPairing() {
        val start = Codec.decode(Codec.audioStart())!!
        assertEquals("pcm16le", Codec.field(start, "codec")); assertEquals("16000", Codec.field(start, "rate"))
        assertEquals("001234", Codec.field(Codec.decode(Codec.confirm("001234"))!!, "code"))
    }
}
