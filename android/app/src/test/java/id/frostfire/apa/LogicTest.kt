package id.frostfire.apa

import id.frostfire.apa.briefing.Speech
import id.frostfire.apa.briefing.WakeTime
import id.frostfire.apa.health.HealthPayload
import id.frostfire.apa.health.Metric
import id.frostfire.apa.health.Sleep
import id.frostfire.apa.listener.AlarmDismissDetector
import id.frostfire.apa.listener.IdempotencyKey
import id.frostfire.apa.listener.NotificationFilter
import id.frostfire.apa.listener.Scrubber
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime

class ScrubberTest {
    @Test fun amountsSurvive() {
        assertEquals("Pembayaran QRIS Rp18.000 ke KOPI berhasil.", Scrubber.scrub("Pembayaran QRIS Rp18.000 ke KOPI berhasil."))
        assertEquals("Transfer Rp 1.250.000,50 berhasil", Scrubber.scrub("Transfer Rp 1.250.000,50 berhasil"))
    }

    @Test fun identifiersAreRemoved() {
        assertEquals("NIK [NIK]", Scrubber.scrub("NIK 3171234567890123"))
        assertEquals("kartu [CARD]", Scrubber.scrub("kartu 4111 1111 1111 1111"))
        assertEquals("hubungi [PHONE]", Scrubber.scrub("hubungi 081234567890"))
        assertEquals("hubungi [PHONE]", Scrubber.scrub("hubungi +6281234567890"))
        assertEquals("kirim ke [EMAIL]", Scrubber.scrub("kirim ke budi@example.com"))
        assertEquals("rek [ACCT]", Scrubber.scrub("rek 1234567890"))
    }

    @Test fun nothingSensitiveIsLeftInAMixedMessage() {
        val out = Scrubber.scrub("Transfer Rp 50.000 ke rek 1234567890 a.n. Budi, hubungi 081234567890 atau budi@x.id")
        assertTrue(out.contains("Rp 50.000"))
        assertFalse(out.contains("1234567890"))
        assertFalse(out.contains("081234567890"))
        assertFalse(out.contains("budi@x.id"))
    }
}

class NotificationFilterTest {
    private val bca = "com.bca"

    @Test fun aMovementFromAKnownBankIsAccepted() {
        assertTrue(NotificationFilter.accept(bca, "Pembayaran QRIS Rp18.000 ke KOPI berhasil."))
    }

    @Test fun unknownAppsAreNeverForwarded() {
        assertFalse(NotificationFilter.accept("com.whatsapp", "Rp 50.000"))
    }

    @Test fun oneTimeCodesAreNeverForwarded() {
        assertFalse(NotificationFilter.accept(bca, "Kode OTP 123456 untuk transaksi Rp 50.000, jangan berikan ke siapapun"))
        assertFalse(NotificationFilter.accept(bca, "PIN Anda Rp 10"))
    }

    @Test fun messagesWithoutAnAmountOrBlankAreDropped() {
        assertFalse(NotificationFilter.accept(bca, "Selamat datang kembali"))
        assertFalse(NotificationFilter.accept(bca, ""))
        assertFalse(NotificationFilter.accept(bca, null))
    }

    @Test fun promotionsAreDroppedButASuccessfulPaymentMentioningCashbackIsKept() {
        assertFalse(NotificationFilter.accept("com.gojek.app", "Promo diskon Rp 10.000 untuk kamu"))
        assertTrue(NotificationFilter.accept("com.gojek.app", "Pembayaran Rp 18.000 berhasil, cashback Rp 1.000"))
    }
}

class NotificationFilterPromoTest {
    private val bca = "com.bca"

    @Test fun offersWithAnAmountAreDropped() {
        assertFalse(NotificationFilter.accept(bca, "Cashback hingga Rp 50.000 untuk transaksi pertama! Buruan klaim"))
        assertFalse(NotificationFilter.accept(bca, "Pinjaman hingga Rp 100 juta, ajukan sekarang"))
        assertFalse(NotificationFilter.accept(bca, "Selamat! Anda berhak hadiah Rp 500.000"))
        assertFalse(NotificationFilter.accept(bca, "Transfer ke ANDI Rp 100.000 berhasil. Dapatkan cashback 10% min. transaksi Rp 50.000"))
    }

    @Test fun remindersAndBalanceNotesAreDropped() {
        assertFalse(NotificationFilter.accept(bca, "Tagihan kartu kredit Rp 1.200.000 jatuh tempo 25 Okt"))
        assertFalse(NotificationFilter.accept(bca, "Saldo Anda Rp 1.250.000"))
        assertFalse(NotificationFilter.accept(bca, "Pembayaran Rp 20.000 gagal"))
    }

    @Test fun realPaymentsPassEvenWithAPromoTail() {
        assertTrue(NotificationFilter.accept(bca, "Pembayaran QRIS Rp18.000 ke KOPI KENANGAN berhasil."))
        assertTrue(NotificationFilter.accept(bca, "Anda menerima transfer Rp 50.000 dari ANDI"))
        assertTrue(NotificationFilter.accept(bca, "Pembayaran Rp 50.000 ke Tokopedia berhasil. Dapatkan cashback 10%"))
    }

    @Test fun newBuiltInBanksAndPickedAppsAreListenedToOthersAreNot() {
        val pay = "Pembayaran QRIS Rp18.000 ke KOPI KENANGAN berhasil."
        assertTrue(NotificationFilter.accept("id.bni.wondr", pay))
        assertTrue(NotificationFilter.accept("com.jago.digitalBanking", pay))
        assertFalse(NotificationFilter.accept("id.co.bankjatim.jconnect", pay))
        assertTrue(NotificationFilter.accept("id.co.bankjatim.jconnect", pay, setOf("id.co.bankjatim.jconnect")))
        assertFalse(NotificationFilter.accept("com.instagram.android", pay, setOf("id.co.bankjatim.jconnect")))
    }
}

class BankAppsTest {
    @Test fun bankAndWalletNamesAreRecognised() {
        listOf("Bank Jatim JConnect", "Jago", "wondr by BNI", "Livin' by Mandiri", "BRImo", "blu by BCA Digital", "Jenius", "OCTO Mobile",
            "SeaBank", "GoPay", "DANA", "OVO", "LinkAja").forEach {
            assertTrue(it, id.frostfire.apa.listener.BankApps.looksLikeBank(it))
        }
    }

    @Test fun ordinaryAppsAreNot() {
        listOf("WhatsApp", "Instagram", "Bluetooth Share", "Dance Party", "Bridge Game", "Blue Light Filter", "Danger Zone").forEach {
            assertFalse(it, id.frostfire.apa.listener.BankApps.looksLikeBank(it))
        }
    }
}

class InternationalFilterTest {
    private val bank = "com.bca"

    @Test fun amountsInOtherCurrenciesAreDetected() {
        assertTrue(NotificationFilter.accept(bank, "You paid USD 12.50 to Coffee Shop"))
        assertTrue(NotificationFilter.accept(bank, "Card purchase of \$45.20 at Tesco completed"))
        assertTrue(NotificationFilter.accept(bank, "Payment of 12.50 EUR successful"))
        assertTrue(NotificationFilter.accept(bank, "You sent €30 to Anna"))
        assertTrue(NotificationFilter.accept(bank, "Pembayaran RM 25.00 berhasil"))
        assertTrue(NotificationFilter.accept(bank, "Transfer of 50,000 VND successful"))
        assertTrue(NotificationFilter.accept(bank, "Pembayaran Rp 18.000 berhasil"))
    }

    @Test fun noAmountIsStillIgnored() {
        assertFalse(NotificationFilter.accept(bank, "Your card was used at Tesco"))
        assertFalse(NotificationFilter.accept(bank, "Welcome back"))
    }

    @Test fun englishPromoOtpAndFailureAreDropped() {
        assertFalse(NotificationFilter.accept(bank, "Get cashback up to \$50 on your first order"))
        assertFalse(NotificationFilter.accept(bank, "20% off with voucher, spend USD 30"))
        assertFalse(NotificationFilter.accept(bank, "Your verification code is 123456 for USD 40, do not share it"))
        assertFalse(NotificationFilter.accept(bank, "Payment of \$20 declined"))
        assertFalse(NotificationFilter.accept(bank, "Your statement is ready, total \$120.00"))
        assertFalse(NotificationFilter.accept(bank, "Reminder: payment of GBP 40 is due"))
    }
}

class InternationalBankAppsTest {
    @Test fun internationalBankNamesAreRecognised() {
        listOf("Revolut", "Wise", "PayPal", "Chase Mobile", "HSBC UK", "DBS digibank", "GCash", "Touch n Go eWallet", "Monzo", "N26").forEach {
            assertTrue(it, id.frostfire.apa.listener.BankApps.looksLikeBank(it))
        }
    }

    @Test fun internationalNonBanksAreNot() {
        listOf("Spotify", "Google Maps", "Netflix", "Camera").forEach {
            assertFalse(it, id.frostfire.apa.listener.BankApps.looksLikeBank(it))
        }
    }
}

class LocationPickTest {
    private val now = 10L * 60 * 60 * 1000 * 3

    @Test fun theFreshestUsableFixWins() {
        val a = id.frostfire.apa.location.Fix(-6.2, 106.8, now - 3_000_000)
        val b = id.frostfire.apa.location.Fix(-7.2, 112.7, now - 1_000_000)
        assertEquals(b, id.frostfire.apa.location.LocationPick.best(listOf(a, b), now))
    }

    @Test fun anOldFixIsBetterLeftUnsentThanSentWrong() {
        val old = id.frostfire.apa.location.Fix(-6.2, 106.8, now - id.frostfire.apa.location.LocationPick.MAX_AGE_MS - 1)
        assertEquals(null, id.frostfire.apa.location.LocationPick.best(listOf(old), now))
    }

    @Test fun impossibleOrFutureFixesAreIgnored() {
        val bad = listOf(
            id.frostfire.apa.location.Fix(91.0, 0.0, now), id.frostfire.apa.location.Fix(0.0, 181.0, now),
            id.frostfire.apa.location.Fix(0.0, 0.0, now + 60_000),
        )
        assertEquals(null, id.frostfire.apa.location.LocationPick.best(bad, now))
        assertEquals(null, id.frostfire.apa.location.LocationPick.best(emptyList(), now))
    }
}

class BackgroundHelpTest {
    private val help = id.frostfire.apa.background.BackgroundHelp

    @Test fun makersMapToTheirAutostartScreensAndUnknownMakersToNone() {
        assertTrue(help.targetsFor("Xiaomi").first().pkg == "com.miui.securitycenter")
        assertEquals(help.targetsFor("xiaomi"), help.targetsFor("  Redmi "))  // case and spaces do not matter, Redmi is Xiaomi
        assertTrue(help.targetsFor("samsung").isNotEmpty() && help.targetsFor("OPPO").isNotEmpty() && help.targetsFor("vivo").isNotEmpty())
        assertTrue(help.targetsFor("Google").isEmpty() && help.targetsFor("").isEmpty())
    }

    @Test fun theSwitchIsNamedTheWayTheMakerNamesIt() {
        assertTrue(help.vendorSwitchName("Xiaomi").contains("No restrictions"))
        assertTrue(help.vendorSwitchName("unknown maker").isNotBlank())
    }

    @Test fun syncChoicesAreClampedToTheAllowedOnes() {
        assertEquals(15, help.clampSync(1)); assertEquals(15, help.clampSync(0)); assertEquals(30, help.clampSync(29))
        assertEquals(60, help.clampSync(61)); assertEquals(120, help.clampSync(10_000)); assertEquals(120, help.clampSync(100))
    }

    @Test fun theChecklistShowsTicksAndTheReasonForEachCross() {
        val checks = listOf(id.frostfire.apa.background.BackgroundHelp.Check("A", true, "x"), id.frostfire.apa.background.BackgroundHelp.Check("B", false, "why it matters"))
        assertEquals("✓ A\n✗ B — why it matters", help.checklist(checks))
        assertFalse(help.allGood(checks))
        assertTrue(help.allGood(listOf(id.frostfire.apa.background.BackgroundHelp.Check("A", true, ""))) && help.allGood(emptyList()))
    }
}

class IdempotencyKeyTest {
    @Test fun sameInputSameKeyAndAnyChangeMakesANewOne() {
        val a = IdempotencyKey.of("com.bca", 1000, "Rp 1")
        assertEquals(a, IdempotencyKey.of("com.bca", 1000, "Rp 1"))
        assertNotEquals(a, IdempotencyKey.of("com.bca", 1001, "Rp 1"))
        assertNotEquals(a, IdempotencyKey.of("com.bca", 1000, "Rp 2"))
        assertNotEquals(a, IdempotencyKey.of("id.dana", 1000, "Rp 1"))
        assertEquals(64, a.length)  // fits the server's 8..128 range
    }
}

class AlarmDismissDetectorTest {
    private val morning = LocalDateTime.of(2026, 10, 25, 6, 30)

    @Test fun aDismissedMorningAlarmStartsTheBriefing() {
        assertTrue(AlarmDismissDetector.shouldPlay("com.google.android.deskclock", "alarm", true, morning, false))
        assertTrue(AlarmDismissDetector.shouldPlay("com.huawei.deskclock", null, true, morning, false))
    }

    @Test fun onlyOncePerDayAndOnlyInTheMorning() {
        assertFalse(AlarmDismissDetector.shouldPlay("com.google.android.deskclock", "alarm", true, morning, true))
        assertFalse(AlarmDismissDetector.shouldPlay("com.google.android.deskclock", "alarm", true, morning.withHour(15), false))
        assertFalse(AlarmDismissDetector.shouldPlay("com.google.android.deskclock", "alarm", true, morning.withHour(3), false))
        assertTrue(AlarmDismissDetector.shouldPlay("com.google.android.deskclock", "alarm", true, morning.withHour(4), false))
        assertFalse(AlarmDismissDetector.shouldPlay("com.google.android.deskclock", "alarm", true, morning.withHour(11), false))
    }

    @Test fun otherAppsAndOrdinaryClockNotificationsDoNotCount() {
        assertFalse(AlarmDismissDetector.shouldPlay("com.whatsapp", "alarm", true, morning, false))
        assertFalse(AlarmDismissDetector.shouldPlay("com.google.android.deskclock", "reminder", false, morning, false))
    }
}

class SpeechTest {
    @Test fun shortTextIsOnePiece() = assertEquals(listOf("Halo."), Speech.chunk("Halo."))

    @Test fun longTextIsSplitAtSentencesAndNothingIsLost() {
        val sentence = "Ini satu kalimat berita yang cukup panjang untuk diuji. "
        val text = sentence.repeat(200).trim()
        val parts = Speech.chunk(text, 500)
        assertTrue(parts.size > 5)
        assertTrue(parts.all { it.length <= 500 && it.isNotBlank() })
        assertTrue(parts.dropLast(1).all { it.endsWith(".") })
        assertEquals(text.replace(" ", ""), parts.joinToString("").replace(" ", ""))
    }

    @Test fun aWordLongerThanTheLimitIsStillCut() {
        val parts = Speech.chunk("x".repeat(100), 30)
        assertTrue(parts.all { it.length <= 30 })
        assertEquals(100, parts.sumOf { it.length })
    }

    @Test fun blankInputGivesNothing() = assertEquals(emptyList<String>(), Speech.chunk("   "))
}

class WakeTimeTest {
    @Test fun laterTodayStaysToday() {
        assertEquals(LocalDateTime.of(2026, 10, 25, 6, 30), WakeTime.next(LocalDateTime.of(2026, 10, 25, 5, 0), 6, 30))
    }

    @Test fun alreadyPassedMeansTomorrow() {
        assertEquals(LocalDateTime.of(2026, 10, 26, 6, 30), WakeTime.next(LocalDateTime.of(2026, 10, 25, 7, 0), 6, 30))
    }

    @Test fun exactlyNowMeansTomorrowNotAFireInThePast() {
        assertEquals(LocalDateTime.of(2026, 10, 26, 6, 30), WakeTime.next(LocalDateTime.of(2026, 10, 25, 6, 30), 6, 30))
    }

    @Test fun monthEndRollsOver() {
        assertEquals(LocalDateTime.of(2026, 11, 1, 6, 30), WakeTime.next(LocalDateTime.of(2026, 10, 31, 8, 0), 6, 30))
    }

    @Test(expected = IllegalArgumentException::class) fun invalidTimesAreRefused() {
        WakeTime.next(LocalDateTime.of(2026, 10, 25, 5, 0), 25, 0)
    }
}

class HealthPayloadTest {
    @Test fun validReadingsAreSentAndOutOfRangeOnesAreDroppedNotFatal() {
        val body = HealthPayload.build(
            "phone-1",
            listOf(
                Metric(Instant.parse("2026-10-25T01:00:00Z"), steps = 4200, restingHeartRate = 58, spo2 = 97.0),
                Metric(Instant.parse("2026-10-25T02:00:00Z"), steps = -5, restingHeartRate = 3),  // nothing valid left
            ),
            listOf(Sleep(LocalDate.of(2026, 10, 25), 330, deepPct = 12.0), Sleep(LocalDate.of(2026, 10, 24), 5000)),
        )
        assertEquals("phone-1", body.getString("device_id"))
        assertEquals(1, body.getJSONArray("metrics").length())
        val m = body.getJSONArray("metrics").getJSONObject(0)
        assertEquals(4200L, m.getLong("step_count"))
        assertEquals(58L, m.getLong("resting_heart_rate"))
        assertEquals(1, body.getJSONArray("sleep").length())  // the 5000-minute night was dropped
        assertEquals(330L, body.getJSONArray("sleep").getJSONObject(0).getLong("total_sleep_mins"))
    }

    @Test fun payloadsAreCappedToWhatTheServerAccepts() {
        val many = (0 until 700).map { Metric(Instant.ofEpochSecond(it * 60L), steps = 1) }
        assertEquals(500, HealthPayload.build("d", many, emptyList()).getJSONArray("metrics").length())
    }
}
