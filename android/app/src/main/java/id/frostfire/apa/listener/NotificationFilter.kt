package id.frostfire.apa.listener

/**
 * Decides whether a notification is worth sending to the server at all. Anything dropped here never leaves the phone.
 * The server applies the same rules again (apa.ingestion.bank_parser.classify_skip), so a promotion that slips past one
 * side is still caught by the other.
 */
object NotificationFilter {
    /** Indonesian banks and wallets listened to out of the box. The owner can add more from the settings screen. */
    val PACKAGES = setOf(
        "com.bca", "com.bca.mybca.omni.android", "com.bcadigital.blu",
        "id.co.bankmandiri.livin", "id.bmri.livin", "id.co.bri.brimo",
        "src.com.bni", "id.bni.wondr", "com.jago.digitalBanking", "com.btpn.dc",
        "id.co.cimbniaga.mobile.android", "net.myinfosys.PermataMobileX", "com.bsm.activity2", "com.bnc.finance",
        "com.gojek.app", "com.gojek.gopay", "ovo.id", "id.dana", "com.shopee.id", "com.telkom.mwallet",
    )

    private val amount = Regex("""(?i)(?:Rp\.?|IDR)\s?\d""")
    private val credential = Regex("""(?i)\b(otp|kode verifikasi|kode rahasia|jangan berikan|password|pin|cvv|one[- ]time)\b""")
    private val failed = Regex("""(?i)\b(gagal|dibatalkan|ditolak|failed|declined|expired|kedaluwarsa)\b""")

    /** Marketing. It often carries a "Rp" amount ("cashback hingga Rp 50.000"), so it is dropped before amounts matter. */
    private val promo = Regex(
        """(?i)\b(promo|promosi|diskon|cashback|voucher|kupon|undian|hadiah|gratis|bonus|reward|poin|penawaran|tawaran|""" +
            """flash\s*sale|sale|dapatkan|ajukan|pinjaman|kta|kredit\s+tanpa|cicilan\s+0|bunga\s+\d|s\s*&\s*k|""" +
            """syarat\s+(?:dan|&)\s+ketentuan|buruan|jangan\s+lewatkan|klaim|menang|selamat|berhak|""" +
            """khusus\s+(?:untuk\s+)?anda|referral|ajak\s+teman|investasi|reksa\s*dana|asuransi|upgrade|""" +
            """naikkan\s+limit|pre-?approved|limited|kuota\s+terbatas)\b""",
    )

    /** "hingga Rp 50 juta", "mulai dari Rp 10.000": a ceiling or starting price in an offer, never a payment made. */
    private val offerAmount = Regex(
        """(?i)\b(hingga|sampai\s+dengan|s\.?d\.?|mulai\s+dari|up\s+to|maks(?:imal|\.)?|""" +
            """min(?:imal|\.)?\s+(?:transaksi|belanja|pembelian|top[\s-]?up))\s*(?:Rp\.?|IDR)""",
    )

    /** Bills and reminders: money is mentioned but none moved. */
    private val reminder = Regex(
        """(?i)\b(tagihan|jatuh\s+tempo|pengingat|reminder|minimum\s+payment|segera\s+(?:lunasi|bayar)|belum\s+dibayar|""" +
            """batas\s+waktu\s+pembayaran|cek\s+saldo|info\s+saldo|saldo\s+(?:anda|kamu))\b""",
    )

    /** A confirmation that money actually moved: the only thing allowed to outweigh a promotional word. */
    private val confirmed = Regex(
        """(?is)\b(?:(?:berhasil|sukses)\b.{0,40}\b(?:transfer|pembayaran|pembelian|top[\s-]?up|tarik\s+tunai|kirim|dikirim|""" +
            """dibayar|debit)|(?:transfer|pembayaran|pembelian|top[\s-]?up|tarik\s+tunai|debit|bayar)\b.{0,60}\b""" +
            """(?:berhasil|sukses)|(?:menerima|diterima|dana\s+masuk|uang\s+masuk|transfer\s+masuk)\b)""",
    )

    const val MAX_TEXT = 1000

    /**
     * True only for a notification from a listened-to app (built in, or one the owner picked) that reports money
     * moving: it must mention an amount, must not be a code, a failure, a reminder, a balance note or a promotion.
     */
    fun accept(packageName: String, text: String?, extra: Set<String> = emptySet()): Boolean {
        if (packageName !in PACKAGES && packageName !in extra) return false
        val t = text?.trim().orEmpty()
        if (t.isEmpty() || t.length > MAX_TEXT * 4) return false
        if (credential.containsMatchIn(t)) return false  // never forward a one-time code
        if (failed.containsMatchIn(t)) return false
        if (!amount.containsMatchIn(t)) return false
        val sure = confirmed.containsMatchIn(t)
        if (offerAmount.containsMatchIn(t)) return false
        if (reminder.containsMatchIn(t) && !sure) return false
        if (promo.containsMatchIn(t) && !sure) return false
        return true  // unusual wording still goes to the server: dropping a possible payment is worse than one more check
    }
}
