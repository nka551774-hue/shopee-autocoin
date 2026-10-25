package tw.avianjay.autocoin

// 判斷目前在哪個畫面，以及解析直播間、「看更多」列表的內容（對應電腦版 screens.py）

private val TIME = Regex("""(\d+):(\d{2})""")
private val NUM = Regex("""\d+(?:\.\d+)?""")
private val PURE_NUM = Regex("""\d+(?:\.\d+)?""")

fun num(text: String?): Double? = text?.let { NUM.find(it)?.value?.toDoubleOrNull()?.takeIf(Double::isFinite) }

fun countdown(state: String): Int? {
    val match = TIME.matchEntire(state.trim()) ?: return null
    val minutes = match.groupValues[1].toLongOrNull() ?: return null
    val seconds = match.groupValues[2].toIntOrNull() ?: return null
    if (seconds !in 0..59 || minutes > (Int.MAX_VALUE - seconds) / 60L) return null
    return (minutes * 60 + seconds).toInt()
}

enum class Screen(val label: String) {
    ROOM("直播間"),
    LIVE_PREVIEW("直播預覽"),
    EXPLORE("看更多列表"),
    PANEL("任務獎勵面板"),
    WITHDRAW("提領頁"),
    WHEEL("幸運轉盤"),
    WHEEL_RESULT("轉盤結果"),
    FOLLOW_DIALOG("關注對話框"),
    VIDEO_TAB("直播短影音分頁"),
    HOME("首頁"),
    WEB("網頁"),
    OTHER_APP("其他 App"),
    UNKNOWN("未知"),
}

fun detect(s: Snap): Screen {
    if (s.pkg.isNotEmpty() && s.pkg != SHOPEE) return Screen.OTHER_APP
    // 疊在直播間上面的東西先判斷
    if (s.id("fl_lucky_result") != null) return Screen.WHEEL_RESULT
    if (s.id("tv_confirm") != null && s.text("僅限粉絲！") != null) return Screen.FOLLOW_DIALOG
    if (s.id("draw_title") != null) return Screen.WHEEL
    if (s.text("立即提領") != null) return Screen.WITHDRAW
    if (s.text("待提領蝦幣數") != null) return Screen.PANEL
    if (s.byPrefix("live_card_cover_").isNotEmpty()) return Screen.EXPLORE
    if (s.id("tv_anchor_name") != null || s.id("live_page_anchor_info_view") != null) {
        val pager = s.id("live_cubepage_0")
        val preview = s.text("向上滑動以查看完整內容") != null || (pager != null && pager.bounds.top > 100)
        return if (preview) Screen.LIVE_PREVIEW else Screen.ROOM
    }
    if ("WebPageActivity" in s.activity) return Screen.WEB
    if (s.desc("tab_bar_button_video_and_live") != null) {
        return if (s.desc("click 1 tab") != null) Screen.VIDEO_TAB else Screen.HOME
    }
    return Screen.UNKNOWN
}

/** 「觀看直播任務」或「直播間蝦幣」其中一格。 */
class Task(
    val present: Boolean = false,
    val coin: Double? = null,        // 這一輪可以拿到的蝦幣
    val state: String = "",          // 倒數 mm:ss、「領取」或其他文字
    val button: Pt? = null,
) {
    val seconds: Int? get() = countdown(state)
    val claimable get() = state.trim() == "領取"
}

private fun Snap.task(box: N?): Task {
    if (box == null) return Task()
    val state = find(box, "tv_state_btn")
    val coin = find(box, "tv_coin_num")
    return Task(true, num(coin?.text), state?.text ?: "", state?.center)
}

class Room(
    val anchor: String,
    val title: String,
    val watch: Task,
    val coin: Task,
    val wheel: String,               // 參加／已參加／已結束；空字串＝沒有轉盤
    val wheelButton: Pt?,
    val rains: Map<String, String>,  // luck_rain / super_rain → 倒數文字
    val claimed: String,             // 「成功領取 0.35 Coins」
    val banner: Pt?,                 // 任務獎勵面板入口
    val explore: Pt?,                // 「看更多」
    val close: Pt?,                  // 獨立直播間右上角的 X
) {
    val key get() = anchor.ifEmpty { title }
}

fun readRoom(s: Snap): Room {
    val wheelBox = s.id("box_view_lucky_draw")
    val wheelState = wheelBox?.let { s.find(it, "tv_state_btn") }
    val rains = LinkedHashMap<String, String>()
    for ((rid, name) in listOf("box_view_luck_rain" to "luck_rain", "box_view_super_rain" to "super_rain")) {
        val box = s.id(rid) ?: continue
        val cd = s.find(box, "tv_count_down") ?: s.find(box, "stv_count_down")
        rains[name] = cd?.text ?: ""
    }
    return Room(
        anchor = s.id("tv_anchor_name")?.text ?: "",
        title = s.id("tv_live_title")?.text ?: "",
        watch = s.task(s.id("box_view_watch_live_task")),
        coin = s.task(s.id("box_view_coin_streamer") ?: s.id("box_view_coin_streamer_v2")),
        wheel = if (wheelBox == null) "" else wheelState?.text ?: "?",
        wheelButton = wheelBox?.center,
        rains = rains,
        claimed = s.id("tv_coin_claimed_content")?.text ?: "",
        banner = s.id("iv_banner")?.center,
        explore = s.id("explore_layout")?.center,
        close = s.id("iv_page_close")?.center,
    )
}

/** 「看更多」列表裡的一張直播卡片。 */
class Card(
    val rid: String,
    val center: Pt,
    val height: Int,
    val viewers: String,
    val maxCoins: Double?,           // 卡片上的「最多X」
    val lottery: Boolean,            // 卡片上的「抽獎」
    val streamer: String,
    val title: String,
) {
    val key get() = streamer.ifEmpty { title }
}

// 卡片上的標籤，不是標題
private val CARD_TAGS = setOf("直播限時價", "官方特選賣家", "關注中的賣場", "官方合作直播主", "優惠券", "抽獎")

fun readCards(s: Snap): List<Card> = s.byPrefix("live_card_cover_").map { card ->
    val all = s.sub(card)
    val avatar = all.firstOrNull { it.rid.startsWith("streamer_avatar_") }
    val inAvatar = avatar?.let { it.index until it.end }
    val texts = all.filter { it.text.isNotEmpty() && (inAvatar == null || it.index !in inAvatar) }
    val labels = texts.map { it.text }
    // 標題是頭像那排上面、最下面的一行字；卡片只露出上半部時拿不到
    val body = texts.filter { it.text !in CARD_TAGS && it.bounds.top - card.bounds.top > 200 }
    Card(
        rid = card.rid,
        center = card.center,
        height = card.height,
        viewers = labels.firstOrNull() ?: "",
        maxCoins = labels.firstOrNull { it.startsWith("最多") }?.let(::num),
        lottery = "抽獎" in labels,
        streamer = avatar?.let { s.texts(it).joinToString(" ") } ?: "",
        title = body.maxByOrNull { it.bounds.top }?.text ?: "",
    )
}

/** 找 anchor 正上方或正下方最近的純數字文字（面板上的金額都是這樣排的）。 */
fun numberNear(s: Snap, anchor: N, below: Boolean): Double? {
    val a = anchor.bounds
    var best: Pair<Int, Double>? = null
    for (n in s.trustedNodes) {
        if (!PURE_NUM.matches(n.text)) continue
        val b = n.bounds
        if (b.right < a.left - 200 || b.left > a.right + 200) continue
        val gap = if (below) b.top - a.bottom else a.top - b.bottom
        val value = num(n.text) ?: continue
        if (gap in -20..150 && (best == null || gap < best.first)) best = gap to value
    }
    return best?.second
}
