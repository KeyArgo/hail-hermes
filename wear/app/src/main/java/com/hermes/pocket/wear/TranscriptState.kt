package com.hermes.pocket.wear

data class TranscriptLine(
    val role: String,
    val text: String,
    val isFinal: Boolean
) {
    val formatted: String
        get() = "$role: $text"
}

data class TranscriptState(
    val lines: List<TranscriptLine> = emptyList()
) {
    val formattedLines: List<String>
        get() = lines.map { it.formatted }

    fun onTranscript(
        role: String,
        text: String,
        isFinal: Boolean
    ): TranscriptState {
        val last = lines.lastOrNull()
        // The hub can send the same finished answer twice — a streamed turn finalises the reply and
        // the drain path can finalise it again with identical text. Appending it would show the
        // sentence twice on the watch, so a repeated final replaces nothing and adds nothing.
        if (isFinal && last != null && last.isFinal && last.role == role &&
            last.text.trim() == text.trim()
        ) {
            return this
        }
        val updated = if (last != null && last.role == role && !last.isFinal) {
            val mergedText = if (isFinal) text else last.text + text
            lines.dropLast(1) + TranscriptLine(
                role = role,
                text = mergedText,
                isFinal = isFinal
            )
        } else {
            lines + TranscriptLine(
                role = role,
                text = text,
                isFinal = isFinal
            )
        }
        return TranscriptState(lines = updated.takeLast(MAX_LINES))
    }

    fun startNewTurn(): TranscriptState {
        val last = lines.lastOrNull() ?: return this
        if (last.isFinal) return this
        return TranscriptState(
            lines = lines.dropLast(1) + last.copy(isFinal = true)
        )
    }

    companion object {
        const val MAX_LINES = 40
    }
}
