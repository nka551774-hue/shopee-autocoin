package tw.avianjay.autocoin

import android.content.Context
import org.json.JSONObject
import java.time.LocalDate
import java.time.ZoneId

val REWARD_ZONE: ZoneId = ZoneId.of("Asia/Taipei")

/** 使用者可以調的設定。 */
class Settings(ctx: Context) {
    private val prefs = ctx.getSharedPreferences("settings", Context.MODE_PRIVATE)

    /** 自動提領（待提領 → 我的蝦幣）。 */
    var withdraw: Boolean
        get() = prefs.getBoolean("withdraw", true)
        set(v) = prefs.edit().putBoolean("withdraw", v).apply()

    val minRoomCoins = 0.5          // 卡片上「最多X」低於這個值就不考慮
    val explorePages = 6            // 「看更多」最多往下捲幾次
    val maxRoomMinutes = 75.0       // 保險：單一直播間最多待多久
    val noCoinGraceMs = 60_000L     // 「直播間蝦幣」不見多久就換台（領完、回到直播間時會暫時消失）
    val watchRoomMinutes = 10.5     // 只為觀看任務待的話，每間待多久（每場只算 10 分鐘）
    val panelEveryMinutes = 30.0    // 多久開一次面板，把觀看任務的蝦幣領進待提領
    val withdrawMin = 5.0           // 待提領至少這麼多才提領；收尾那次只要 ≥1
    val withdrawDailyLimit = 3      // 蝦皮規定每天最多提領 3 次
}

class RoomStat {
    var claims = 0
    var coins = 0.0
    var done = false
    var wheel = false      // 試過轉盤
    var wheelGot = false   // 轉盤結果已經記帳
    var watched = false
}

/** 當天的統計（對應電腦版 state.py 的 Day）。 */
class Day(val date: String) {
    var coinClaims = 0
    var coinTotal = 0.0
    var coinLimitHit = false
    var wheelSpins = 0
    var wheelTotal = 0.0
    var wheelDone = false
    var watchTotal = 0.0
    var watchMinutes = 0
    var watchDone = false
    var withdrawals = 0
    var withdrawn = 0.0
    var withdrawalUncertain = false
    var pending = 0.0
    var lastPanel = 0L
    var finalPanel = false
    val rooms = HashMap<String, RoomStat>()

    fun room(key: String) = rooms.getOrPut(key) { RoomStat() }
    fun roomDone(key: String) = key.isNotEmpty() && rooms[key]?.done == true
    fun flag(key: String, pick: (RoomStat) -> Boolean) = rooms[key]?.let(pick) ?: false

    fun toJson(): JSONObject = JSONObject().apply {
        put("date", date)
        put("coinClaims", coinClaims); put("coinTotal", coinTotal); put("coinLimitHit", coinLimitHit)
        put("wheelSpins", wheelSpins); put("wheelTotal", wheelTotal); put("wheelDone", wheelDone)
        put("watchTotal", watchTotal); put("watchMinutes", watchMinutes); put("watchDone", watchDone)
        put("withdrawals", withdrawals); put("withdrawn", withdrawn); put("pending", pending)
        put("withdrawalUncertain", withdrawalUncertain)
        put("lastPanel", lastPanel); put("finalPanel", finalPanel)
        put("rooms", JSONObject().apply {
            for ((k, r) in rooms) put(k, JSONObject().apply {
                put("claims", r.claims); put("coins", r.coins); put("done", r.done)
                put("wheel", r.wheel); put("wheelGot", r.wheelGot); put("watched", r.watched)
            })
        })
    }

    companion object {
        private fun amount(o: JSONObject, key: String): Double {
            if (!o.has(key)) return 0.0
            val raw = o.get(key)
            require(raw is Number) { "$key 金額格式無效" }
            return raw.toDouble().also { require(it.isFinite() && it >= 0) { "$key 金額無效" } }
        }

        private fun count(o: JSONObject, key: String): Int {
            val value = amount(o, key)
            require(value <= Int.MAX_VALUE && value % 1.0 == 0.0) { "$key 次數無效" }
            return value.toInt()
        }

        private fun flag(o: JSONObject, key: String): Boolean {
            if (!o.has(key)) return false
            val raw = o.get(key)
            require(raw is Boolean) { "$key 旗標格式無效" }
            return raw
        }

        private fun timestamp(o: JSONObject, key: String): Long {
            if (!o.has(key)) return 0
            val raw = o.get(key)
            require(raw is Number) { "$key 時間格式無效" }
            val value = raw.toString().toLongOrNull()
            require(value != null && value >= 0) { "$key 時間無效" }
            return value
        }

        fun fromJson(o: JSONObject) = Day(LocalDate.parse(o.getString("date")).toString()).apply {
            coinClaims = count(o, "coinClaims"); coinTotal = amount(o, "coinTotal")
            coinLimitHit = flag(o, "coinLimitHit")
            wheelSpins = count(o, "wheelSpins"); wheelTotal = amount(o, "wheelTotal")
            wheelDone = flag(o, "wheelDone")
            watchTotal = amount(o, "watchTotal"); watchMinutes = count(o, "watchMinutes")
            watchDone = flag(o, "watchDone")
            withdrawals = count(o, "withdrawals"); withdrawn = amount(o, "withdrawn")
            withdrawalUncertain = flag(o, "withdrawalUncertain")
            pending = amount(o, "pending")
            lastPanel = timestamp(o, "lastPanel"); finalPanel = flag(o, "finalPanel")
            val rs = if (o.has("rooms")) o.getJSONObject("rooms") else JSONObject()
            for (k in rs.keys()) {
                val r = rs.getJSONObject(k)
                rooms[k] = RoomStat().apply {
                    claims = count(r, "claims"); coins = amount(r, "coins"); done = flag(r, "done")
                    wheel = flag(r, "wheel"); wheelGot = flag(r, "wheelGot")
                    watched = flag(r, "watched")
                }
            }
            require(lastPanel >= 0) { "面板時間無效" }
        }
    }
}

class Store(ctx: Context, private val dateProvider: () -> LocalDate = { LocalDate.now(REWARD_ZONE) }) {
    private val prefs = ctx.getSharedPreferences("state", Context.MODE_PRIVATE)
    private var failure: String? = null
    private var current: Day = load()

    private fun today() = dateProvider().toString()

    private fun nextDay(previous: Day, date: String) = Day(date).apply {
        // A transaction with an unknown outcome is not resolved by midnight or a restart.
        withdrawalUncertain = previous.withdrawalUncertain
        pending = previous.pending
    }

    private fun load(): Day {
        val date = today()
        val loaded = try {
            prefs.getString("day", null)?.let { Day.fromJson(JSONObject(it)) } ?: return Day(date)
        } catch (e: Exception) {
            failure = "統計資料無效，已停止所有自動操作；請先修復原始資料"
            AppLog.e(failure!!, e)
            // This placeholder is only for rendering the error. day/save must reject it.
            return Day(date)
        }
        if (loaded.date > date) {
            failure = "系統日期早於已儲存的統計日期 ${loaded.date}，已停止；請修正日期後重新開始"
            return loaded
        }
        return if (loaded.date == date) loaded else nextDay(loaded, date)
    }

    @Synchronized
    fun requireReady() {
        if (failure == null && current.date > today()) {
            failure = "系統日期倒退，已停止並保留原有配額；請修正日期後重新開始"
        }
        failure?.let { throw StopBot(it) }
    }

    /** Only move forwards; unresolved transactions remain blocked across days. */
    @get:Synchronized
    val day: Day
        get() {
            requireReady()
            val date = today()
            if (current.date < date) {
                AppLog.i("換日了：${summary()}")
                current = nextDay(current, date)
                save()
            }
            if (current.coinClaims >= 100) current.coinLimitHit = true
            if (current.wheelSpins >= 10) current.wheelDone = true
            return current
        }

    @Synchronized
    fun save() {
        requireReady()
        // Persist transaction markers before tapping; apply() can lose them on process death.
        if (!prefs.edit().putString("day", current.toJson().toString()).commit()) {
            throw StopBot("統計無法儲存，停止以免重複操作")
        }
    }

    fun summary(): String {
        failure?.let { return "已停止：$it（原始資料未覆寫）" }
        val d = current
        val notice = if (d.withdrawalUncertain) "｜提領結果待核對（自動提領已鎖定）" else ""
        return "${d.date} 直播間蝦幣 ${d.coinClaims} 次 +%.2f｜轉盤 ${d.wheelSpins} 次 +%.2f｜觀看任務 ${d.watchMinutes} 分鐘 +%.2f｜提領 ${d.withdrawals}/3 次 %.0f｜待提領 %.2f"
            .format(d.coinTotal, d.wheelTotal, d.watchTotal, d.withdrawn, d.pending) + notice
    }
}
