package id.frostfire.apa.background

import kotlin.math.abs

/**
 * What a phone must allow for this app to keep working while it is closed, and where each maker hides the switch.
 * Pure logic (no Android classes), so it is unit-tested; the settings screen does the actual opening.
 *
 * The vendor screens are the ones the community lists at dontkillmyapp.com. Makers rename and move them between
 * versions, so the screen tries each in turn and falls back to the app's own system page; none is guaranteed.
 */
object BackgroundHelp {
    data class Target(val pkg: String, val cls: String)

    private val xiaomi = listOf(
        Target("com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity"),
        Target("com.miui.powerkeeper", "com.miui.powerkeeper.ui.HiddenAppsConfigActivity"),
    )
    private val oppo = listOf(
        Target("com.coloros.safecenter", "com.coloros.safecenter.permission.startup.StartupAppListActivity"),
        Target("com.oppo.safe", "com.oppo.safe.permission.startup.StartupAppListActivity"),
        Target("com.oplus.battery", "com.oplus.powermanager.fuelgaue.PowerUsageModelActivity"),
    )
    private val vivo = listOf(
        Target("com.vivo.permissionmanager", "com.vivo.permissionmanager.activity.BgStartUpManagerActivity"),
        Target("com.iqoo.secure", "com.iqoo.secure.ui.phoneoptimize.AddWhiteListActivity"),
    )
    private val huawei = listOf(
        Target("com.huawei.systemmanager", "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"),
        Target("com.huawei.systemmanager", "com.huawei.systemmanager.optimize.process.ProtectActivity"),
    )
    private val samsung = listOf(Target("com.samsung.android.lool", "com.samsung.android.sm.battery.ui.BatteryActivity"))
    private val asus = listOf(Target("com.asus.mobilemanager", "com.asus.mobilemanager.autostart.AutoStartActivity"))
    private val transsion = listOf(Target("com.transsion.phonemaster", "com.cyin.himgr.autostart.AutoStartActivity"))

    /** The maker's autostart / background-battery screens to try, best first. Empty when the maker is not known. */
    fun targetsFor(manufacturer: String): List<Target> {
        val m = manufacturer.trim().lowercase()
        return when {
            m in setOf("xiaomi", "redmi", "poco", "blackshark") -> xiaomi
            m in setOf("oppo", "realme", "oneplus") -> oppo
            m in setOf("vivo", "iqoo") -> vivo
            m in setOf("huawei", "honor") -> huawei
            m == "samsung" -> samsung
            m == "asus" -> asus
            m in setOf("tecno", "infinix", "itel", "transsion") -> transsion
            else -> emptyList()
        }
    }

    /** What the maker calls the switch, for the button's label. */
    fun vendorSwitchName(manufacturer: String): String = when (manufacturer.trim().lowercase()) {
        "xiaomi", "redmi", "poco", "blackshark" -> "Autostart and Battery saver: No restrictions"
        "oppo", "realme", "oneplus" -> "Allow auto-start and background activity"
        "vivo", "iqoo" -> "Auto-start and background battery usage"
        "huawei", "honor" -> "App launch: manage manually"
        "samsung" -> "Battery: not optimized, do not put in sleeping apps"
        else -> "Auto-start and battery saving"
    }

    val SYNC_CHOICES = listOf(15, 30, 60, 120)  // minutes; Android's own floor for periodic work is 15

    fun clampSync(minutes: Int): Int = SYNC_CHOICES.minByOrNull { abs(it - minutes) } ?: SYNC_CHOICES.first()

    data class Check(val label: String, val ok: Boolean, val why: String)

    /** The checklist as text: a tick or a cross, and for a cross the reason it matters. */
    fun checklist(checks: List<Check>): String =
        checks.joinToString("\n") { (if (it.ok) "✓ " else "✗ ") + it.label + (if (it.ok) "" else " — ${it.why}") }

    fun allGood(checks: List<Check>): Boolean = checks.all { it.ok }
}
