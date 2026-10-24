package tw.avianjay.autocoin

import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo

const val SHOPEE = "com.shopee.tw"

/** 一個 UI 節點的快照。讀完就不再碰 AccessibilityNodeInfo，之後的查詢都在記憶體裡做。 */
class N(
    val rid: String,
    val text: String,
    val desc: String,
    val bounds: Rect,
    val clickable: Boolean,
    val index: Int,
) {
    /** 自己和子孫在 [Snap.nodes] 裡的範圍是 [index, end)。 */
    var end: Int = index + 1
    val cx get() = bounds.centerX()
    val cy get() = bounds.centerY()
    val center get() = Pt(cx, cy)
    val width get() = bounds.width()
    val height get() = bounds.height()
    override fun toString() = "N($rid '$text' $bounds)"
}

data class Pt(val x: Int, val y: Int)

/** 一次讀取的畫面，只保留蝦皮自己的節點（跟電腦版的 uiautomator dump 看到的一樣）。 */
class Snap(val nodes: List<N>, val pkg: String, val activity: String) {
    // A screen is immutable; build the frequently used ID lookup once per snapshot.
    private val byId by lazy(LazyThreadSafetyMode.NONE) {
        HashMap<String, N>(nodes.size).apply {
            for (n in nodes) if (n.rid.isNotEmpty()) putIfAbsent(n.rid, n)
        }
    }
    fun id(rid: String) = byId[rid]
    fun byPrefix(prefix: String) = nodes.filter { it.rid.startsWith(prefix) }
    fun text(t: String) = nodes.firstOrNull { it.text == t }
    fun desc(d: String) = nodes.firstOrNull { it.desc == d }
    fun sub(n: N): List<N> = nodes.subList(n.index, n.end)
    fun find(n: N, rid: String) = sub(n).firstOrNull { it.rid == rid }
    fun texts(n: N) = sub(n).map { it.text }.filter { it.isNotEmpty() }

    /** 全部文字，但略過指定容器底下的（例如聊天室，觀眾可以打任何字）。 */
    fun textsExcluding(ids: Collection<String>): List<String> {
        val texts = ArrayList<String>()
        var i = 0
        while (i < nodes.size) {
            val n = nodes[i]
            if (n.rid in ids) i = n.end else {
                if (n.text.isNotEmpty()) texts.add(n.text)
                i++
            }
        }
        return texts
    }

    /** 給除錯用的文字版 dump。 */
    fun dump(): String = buildString {
        for (n in nodes) {
            if (n.rid.isEmpty() && n.text.isEmpty() && n.desc.isEmpty()) continue
            append("${n.rid.ifEmpty { "-" }}\t${n.text}\t${n.desc}\t${n.bounds.toShortString()}")
            if (n.clickable) append("\tC")
            append('\n')
        }
    }

    companion object {
        fun build(roots: List<AccessibilityNodeInfo>, pkg: String, activity: String): Snap {
            val out = ArrayList<N>(800)
            for (r in roots) walk(r, out, 0)
            return Snap(out, pkg, activity)
        }

        private fun walk(info: AccessibilityNodeInfo, out: MutableList<N>, depth: Int) {
            if (Thread.currentThread().isInterrupted) throw InterruptedException("讀取畫面已停止")
            check(out.size < 20_000) { "UI 節點過多，暫停操作" }
            if (depth > 80 || !info.isVisibleToUser) return
            val rect = Rect()
            info.getBoundsInScreen(rect)
            val n = N(
                // RN 的 testID 沒有「套件:id/」前綴，substringAfter 找不到時會原樣回傳
                rid = info.viewIdResourceName?.substringAfter(":id/") ?: "",
                text = info.text?.toString() ?: "",
                desc = info.contentDescription?.toString() ?: "",
                bounds = rect,
                clickable = info.isClickable,
                index = out.size,
            )
            out.add(n)
            for (i in 0 until info.childCount) {
                val child = info.getChild(i) ?: continue
                try { walk(child, out, depth + 1) } finally { child.recycle() }
            }
            n.end = out.size
        }
    }
}
