package com.hermes.pocket.wear

import org.junit.Assert.*
import org.junit.Test

class DiscoveryTest {
    private val beacon = """{"svc":"hail-hermes","v":1,"name":"Office","port":8767,"fp":"abc"}"""
    @Test fun parsesBeaconAndSender() {
        assertEquals(Computer("Office", "192.0.2.2", 8767, "abc"), Discovery.parse(beacon, "192.0.2.2"))
    }
    @Test fun rejectsMalformedOrInvalidBeacons() {
        for (bad in listOf("{}", "{", beacon.replace("hail-hermes", "other"), beacon.replace("\"v\":1", "\"v\":2"), beacon.replace("8767", "0"), beacon.replace("8767", "\"8767\""), beacon.replace("abc", " "), beacon.replace("Office", " "))) {
            assertNull(bad, Discovery.parse(bad, "192.0.2.2"))
        }
    }
    @Test fun prefersSavedFingerprint() {
        val a = Computer("A", "1.2.3.4", 1, "a")
        val b = Computer("B", "1.2.3.5", 2, "b")
        assertEquals(b, Discovery.preferred(listOf(a, b), "b"))
        assertNull(Discovery.preferred(listOf(a), "b"))
    }
    @Test fun timeoutAndRetry() {
        val machine = SearchMachine()
        machine.start(100)
        machine.tick(30099)
        assertEquals(SearchState.Looking, machine.state)
        machine.tick(30100)
        assertEquals(SearchState.NotFound, machine.state)
        machine.start(40000)
        assertEquals(SearchState.Looking, machine.state)
        machine.stop()
        machine.tick(100000)
        assertEquals(SearchState.Idle, machine.state)
    }
    @Test fun choicesSurviveTimeout() {
        val machine = SearchMachine()
        val computers = listOf(Computer("A", "1.2.3.4", 1, "a"))
        machine.start(0)
        machine.choices(computers)
        machine.tick(30000)
        assertEquals(SearchState.Choices(computers), machine.state)
    }

    @Test fun oneComputerIsChosenWhenNoFingerprintIsRemembered() {
        val dawn = Computer("dawn", "192.0.2.38", 8767, "abc123")
        // A reinstall leaves no saved fingerprint; with a single computer on the network the
        // watch must connect instead of searching until it times out.
        assertEquals(dawn, Discovery.preferred(listOf(dawn), null))
        assertEquals(dawn, Discovery.preferred(listOf(dawn), ""))
    }

    @Test fun twoComputersAndNoFingerprintStaysAmbiguous() {
        val a = Computer("a", "1.1.1.1", 8767, "f1")
        val b = Computer("b", "2.2.2.2", 8767, "f2")
        assertEquals(null, Discovery.preferred(listOf(a, b), null))
    }

    @Test fun aRememberedFingerprintStillWins() {
        val a = Computer("a", "1.1.1.1", 8767, "f1")
        val b = Computer("b", "2.2.2.2", 8767, "f2")
        assertEquals(b, Discovery.preferred(listOf(a, b), "f2"))
    }
}
