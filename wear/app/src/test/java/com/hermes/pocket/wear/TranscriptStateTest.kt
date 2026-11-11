package com.hermes.pocket.wear

import org.junit.Assert.*
import org.junit.Test

class TranscriptStateTest {
    @Test
    fun deltasAppendToSingleDraftAndFinalReplacesWithFullText() {
        var state = TranscriptState()
        state = state.onTranscript(role = "assistant", text = "Hello ", isFinal = false)
        assertEquals(1, state.lines.size)
        assertEquals("Hello ", state.lines[0].text)
        assertFalse(state.lines[0].isFinal)

        state = state.onTranscript(role = "assistant", text = "world", isFinal = false)
        assertEquals(1, state.lines.size)
        assertEquals("Hello world", state.lines[0].text)
        assertFalse(state.lines[0].isFinal)

        state = state.onTranscript(role = "assistant", text = "Hello world", isFinal = true)
        assertEquals(1, state.lines.size)
        assertEquals("Hello world", state.lines[0].text)
        assertTrue(state.lines[0].isFinal)
    }

    @Test
    fun newUserTurnStartsNewLinesForUserAndAssistant() {
        var state = TranscriptState()

        state = state.onTranscript(role = "user", text = "What ", isFinal = false)
        state = state.onTranscript(role = "user", text = "is 2+2?", isFinal = false)
        state = state.onTranscript(role = "user", text = "What is 2+2?", isFinal = true)
        assertEquals(1, state.lines.size)
        assertEquals("What is 2+2?", state.lines[0].text)

        state = state.onTranscript(role = "assistant", text = "It is ", isFinal = false)
        state = state.onTranscript(role = "assistant", text = "4.", isFinal = false)
        state = state.onTranscript(role = "assistant", text = "It is 4.", isFinal = true)
        assertEquals(2, state.lines.size)
        assertEquals("What is 2+2?", state.lines[0].text)
        assertEquals("It is 4.", state.lines[1].text)

        state = state.onTranscript(role = "user", text = "Thanks", isFinal = true)
        assertEquals(3, state.lines.size)
        assertEquals("Thanks", state.lines[2].text)

        state = state.onTranscript(role = "assistant", text = "You're ", isFinal = false)
        state = state.onTranscript(role = "assistant", text = "welcome!", isFinal = false)
        state = state.onTranscript(role = "assistant", text = "You're welcome!", isFinal = true)
        assertEquals(4, state.lines.size)
        assertEquals("You're welcome!", state.lines[3].text)
    }

    @Test
    fun consecutiveAssistantTurnsStartNewLinesAfterFinalReply() {
        var state = TranscriptState()
        state = state.onTranscript(role = "assistant", text = "First reply.", isFinal = true)
        assertEquals(1, state.lines.size)

        state = state.onTranscript(role = "assistant", text = "Second ", isFinal = false)
        assertEquals(2, state.lines.size)
        assertEquals("First reply.", state.lines[0].text)
        assertTrue(state.lines[0].isFinal)
        assertEquals("Second ", state.lines[1].text)
        assertFalse(state.lines[1].isFinal)

        state = state.onTranscript(role = "assistant", text = "Second reply.", isFinal = true)
        assertEquals(2, state.lines.size)
        assertEquals("Second reply.", state.lines[1].text)
        assertTrue(state.lines[1].isFinal)
    }

    @Test
    fun startNewTurnFinalizesOpenDraftSoNextMessageStartsFreshLine() {
        var state = TranscriptState()
        state = state.onTranscript(role = "assistant", text = "partial ", isFinal = false)
        state = state.startNewTurn()
        assertTrue(state.lines.single().isFinal)
        state = state.onTranscript(role = "assistant", text = "next ", isFinal = false)
        assertEquals(2, state.lines.size)
        assertEquals("partial ", state.lines[0].text)
        assertEquals("next ", state.lines[1].text)
    }

    @Test
    fun keepsAtMost40Lines() {
        var state = TranscriptState()
        for (i in 1..25) {
            state = state.onTranscript(role = "user", text = "User $i", isFinal = true)
            state = state.onTranscript(role = "assistant", text = "Assistant $i", isFinal = true)
        }
        assertEquals(40, state.lines.size)
        assertEquals("User 6", state.lines.first().text)
        assertEquals("Assistant 25", state.lines.last().text)
        assertEquals(40, state.formattedLines.size)
        assertEquals("assistant: Assistant 25", state.formattedLines.last())
    }

    @Test
    fun a_finished_answer_arriving_twice_is_shown_once() {
        // Observed on the wire: a streamed turn finalises the reply, then the drain path finalises
        // the same text again. Before this, the watch appended it and showed the sentence twice.
        var state = TranscriptState()
        state = state.onTranscript(role = "assistant", text = "Hello ", isFinal = false)
        state = state.onTranscript(role = "assistant", text = "Hello world.", isFinal = true)
        state = state.onTranscript(role = "assistant", text = "Hello world.", isFinal = true)
        assertEquals(1, state.lines.size)
        assertEquals("Hello world.", state.lines.single().text)
    }
}
