package id.frostfire.apa.listener

/**
 * Removes personal identifiers from a notification BEFORE it is stored or sent (blueprint 4.1: the sanitising
 * happens on the device). Mirrors the server's sanitizer so nothing sensitive leaves the phone. Amounts such as
 * "Rp 1.250.000" are untouched: no money amount has 13+ digits.
 */
object Scrubber {
    private val nik = Regex("""\b\d{16}\b""")
    private val card = Regex("""\b(?:\d[ -]?){13,19}\b""")
    private val phone = Regex("""(?<![\w+])\+?(?:62|0)8\d{8,11}(?!\d)""")
    private val email = Regex("""[\w.+-]+@[\w-]+\.[\w.-]+""")
    private val account = Regex("""(?i)\b(rek(?:ening)?|acc(?:ount)?|no\.?)\s*[:#]?\s*\d{8,}""")

    /** Order matters: the 16-digit NIK is replaced before the generic card pattern can claim it. */
    fun scrub(text: String): String {
        var t = text
        t = nik.replace(t, "[NIK]")
        t = phone.replace(t, "[PHONE]")  // before the card pattern: a phone number would otherwise look like a card
        t = card.replace(t, "[CARD]")
        t = email.replace(t, "[EMAIL]")
        t = account.replace(t) { m -> "${m.groupValues[1]} [ACCT]" }
        return t
    }
}
