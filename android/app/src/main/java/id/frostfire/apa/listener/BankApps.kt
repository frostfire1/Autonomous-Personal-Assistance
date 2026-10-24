package id.frostfire.apa.listener

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager

/** An installed app that looks like a bank or e-wallet. */
data class BankApp(val packageName: String, val label: String, val builtIn: Boolean)

/**
 * Finds the owner's banking and wallet apps by what is installed, so no package name has to be known in advance (Bank
 * Jatim, Jago, Wondr and any other bank are found by their name). The owner confirms the list in the settings screen.
 */
object BankApps {
    /** Words in an app's name that say it is a bank or wallet. */
    val KEYWORDS = listOf(
        "bank", "bca", "bni", "wondr", "bri", "brimo", "mandiri", "livin", "jago", "jatim", "jconnect", "jenius", "blu",
        "octo", "cimb", "permata", "danamon", "bsi", "syariah", "seabank", "neo", "allo", "superbank", "ovo", "dana",
        "gopay", "linkaja", "shopeepay", "digibank", "ocbc", "btn", "mega", "maybank", "panin", "uob", "sinarmas",
        "bjb", "bpd", "nobu", "aladin", "line bank", "krom", "saqu", "motion",
        // international banks, cards and wallets
        "wallet", "pay", "card", "credit", "finance", "revolut", "wise", "paypal", "chase", "citi", "hsbc", "dbs", "grab",
        "paytm", "gcash", "maya", "touch n go", "tng", "kakao", "toss", "paypay", "rakuten", "wells", "barclays", "monzo",
        "n26", "ing", "santander", "bbva",
    )

    /** Apps in the list that are not banks even though a keyword appears in their name. */
    private val NOT_BANKS = listOf("danamon_game", "dance", "bridge", "bribe", "bluetooth", "blue", "neon", "danger", "sandana")

    fun looksLikeBank(label: String): Boolean {
        val l = label.lowercase()
        if (NOT_BANKS.any { it in l }) return false
        return KEYWORDS.any { k -> Regex("""(^|[^a-z])${Regex.escape(k)}([^a-z]|$)""").containsMatchIn(l) }
    }

    /** Every launchable installed app, so the owner can tick any app of any country. */
    fun scanAll(ctx: Context): List<BankApp> {
        val pm = ctx.packageManager
        val launcher = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        return pm.queryIntentActivities(launcher, PackageManager.MATCH_ALL)
            .map { BankApp(it.activityInfo.packageName, it.loadLabel(pm).toString(), it.activityInfo.packageName in NotificationFilter.PACKAGES) }
            .distinctBy { it.packageName }
            .sortedBy { it.label.lowercase() }
    }

    /** Launchable installed apps that look like banks or wallets, plus any built-in one that is installed. */
    fun scan(ctx: Context): List<BankApp> = scanAll(ctx).filter { it.builtIn || looksLikeBank(it.label) }
}
