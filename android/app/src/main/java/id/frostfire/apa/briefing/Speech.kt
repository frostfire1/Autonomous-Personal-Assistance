package id.frostfire.apa.briefing

/** Android's TextToSpeech refuses more than ~4000 characters at once, so long text is spoken in pieces. */
object Speech {
    const val MAX_UTTERANCE = 3500

    /** Splits at sentence ends where possible so a pause falls where a person would pause. Never returns blanks. */
    fun chunk(text: String, max: Int = MAX_UTTERANCE): List<String> {
        require(max > 20) { "max too small" }
        val out = mutableListOf<String>()
        var rest = text.trim()
        while (rest.length > max) {
            var cut = rest.lastIndexOf(". ", max).takeIf { it > max / 3 } ?: rest.lastIndexOf(' ', max)
            if (cut <= 0) cut = max
            val end = if (rest.startsWith(". ", cut)) cut + 1 else cut
            out += rest.substring(0, end).trim()
            rest = rest.substring(end).trim()
        }
        if (rest.isNotEmpty()) out += rest
        return out.filter { it.isNotBlank() }
    }
}
