package com.hermes.pocket.wear

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class CharacterCodecTest {
    @Test
    fun parseCharacterAcceptsHermesAndNousAndUpdatesFlow() {
        val nousObj = Codec.decode("""{"v":2,"type":"character","id":"nous"}""")
        assertNotNull(nousObj)
        val nous = Codec.parseCharacter(nousObj!!)
        assertNotNull(nous)
        assertEquals(Codec.CHARACTER_NOUS, nous!!.id)
        assertEquals("mascot-nous", nous.assetDir)
        assertEquals(Codec.CHARACTER_NOUS, Codec.characterFlow.value)

        val hermesObj = Codec.decode(Codec.character("hermes"))
        assertNotNull(hermesObj)
        val hermes = Codec.parseCharacter(hermesObj!!)
        assertNotNull(hermes)
        assertEquals(Codec.CHARACTER_HERMES, hermes!!.id)
        assertEquals("mascot", hermes.assetDir)
        assertEquals(Codec.CHARACTER_HERMES, Codec.characterFlow.value)
    }

    @Test
    fun nousSkinsMapToTheirOwnAssetDirs() {
        assertEquals("mascot-nous-light", Codec.characterAssetDir("nous-light"))
        assertEquals("mascot-nous-dark", Codec.characterAssetDir("nous-dark"))
        assertEquals("mascot", Codec.characterAssetDir("hermes"))
    }

    @Test
    fun parseCharacterRejectsUnknownOrWrongType() {
        val unknown = Codec.decode("""{"v":2,"type":"character","id":"apollo"}""")
        assertNotNull(unknown)
        assertNull(Codec.parseCharacter(unknown!!))

        val missing = Codec.decode("""{"v":2,"type":"character"}""")
        assertNotNull(missing)
        assertNull(Codec.parseCharacter(missing!!))

        val otherType = Codec.decode("""{"v":2,"type":"state","screen":"idle"}""")
        assertNotNull(otherType)
        assertNull(Codec.parseCharacter(otherType!!))
    }
}
