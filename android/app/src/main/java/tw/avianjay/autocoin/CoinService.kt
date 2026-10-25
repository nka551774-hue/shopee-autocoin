package tw.avianjay.autocoin

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.app.KeyguardManager
import android.content.Intent
import android.graphics.Color
import android.graphics.Path
import android.graphics.PixelFormat
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.view.Gravity
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.widget.TextView
import java.io.File
import java.lang.ref.WeakReference
import java.util.concurrent.CountDownLatch
import java.util.concurrent.ExecutionException
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 無障礙服務：讀蝦皮的畫面、用手勢點座標（跟 MacroDroid 按座標一樣），
 * 並在畫面最下方放一條小狀態列（點它可以開始／停止，執行中會讓螢幕保持亮著）。
 */
class CoinService : AccessibilityService(), Phone {

    companion object {
        private const val STOPPED = "蝦幣 ■ 已停止（點這裡開始）"

        @Volatile private var serviceRef = WeakReference<CoinService>(null)
        var instance: CoinService?
            get() = serviceRef.get()
            private set(value) { serviceRef = WeakReference(value) }
    }

    private val main = Handler(Looper.getMainLooper())
    @Volatile private var worker: Thread? = null
    @Volatile private var stopping = false
    @Volatile private var toast = ""
    @Volatile private var shopeeWindowClass = ""
    @Volatile var statusText = "已停止"
        private set

    private var badge: TextView? = null
    private var badgeParams: WindowManager.LayoutParams? = null

    val running get() = worker?.isAlive == true

    override fun onServiceConnected() {
        AppLog.init(this)
        instance = this
        showBadge()
        AppLog.i("無障礙服務已連線")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent) {
        when (event.eventType) {
            AccessibilityEvent.TYPE_NOTIFICATION_STATE_CHANGED ->
                if (event.packageName?.toString() == SHOPEE && event.className?.contains("Toast") == true) {
                    toast = event.text.joinToString("")
                }
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED ->
                if (event.packageName?.toString() == SHOPEE) shopeeWindowClass = event.className?.toString() ?: ""
            else -> Unit
        }
    }

    override fun onInterrupt() = stopBot()

    override fun onUnbind(intent: Intent?): Boolean {
        stopBot()
        hideBadge()
        instance = null
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        stopBot()
        hideBadge()
        if (instance === this) instance = null
        super.onDestroy()
    }

    // ---------------- 開始 / 停止 ----------------
    @Synchronized
    fun startBot() {
        // Keep ownership until the old worker has actually returned. Otherwise a quick
        // stop/start can leave two threads issuing gestures and writing the same state.
        if (worker != null) return
        stopping = false
        clearToast()
        val bot = Bot(this, Store(this), Settings(this)) { setStatus(it) }
        val thread = Thread({
            try {
                bot.run()
            } finally {
                synchronized(this@CoinService) {
                    if (worker === Thread.currentThread()) {
                        worker = null
                        stopping = false
                        showStopped()
                    }
                }
            }
        }, "autocoin")
        worker = thread
        setStatus("執行中")
        keepScreenOn(true)
        thread.start()
    }

    @Synchronized
    fun stopBot() {
        val thread = worker
        if (thread == null) {
            showStopped()
            return
        }
        stopping = true
        statusText = "正在停止"
        thread.interrupt()
        main.post {
            if (stopping) badge?.text = "蝦幣 ■ 正在停止"
            if (stopping || !running) keepScreenOn(false)
        }
    }

    @Synchronized
    private fun setStatus(text: String) {
        if (stopping) return
        statusText = text
        main.post { if (running && !stopping && statusText == text) badge?.text = getString(R.string.status_running, text) }
    }

    private fun showStopped() {
        statusText = "已停止"
        main.post {
            if (!running) {
                badge?.text = STOPPED
                keepScreenOn(false)
            }
        }
    }

    // ---------------- 狀態列浮窗 ----------------
    private fun showBadge() {
        if (badge != null) return
        val wm = getSystemService(WindowManager::class.java)
        val view = TextView(this).apply {
            textSize = 11f
            setTextColor(Color.WHITE)
            setBackgroundColor(0xAA000000.toInt())
            setPadding(24, 6, 24, 6)
            maxLines = 1
            text = STOPPED
            setOnClickListener { if (running) stopBot() else startBot() }
        }
        // 放在最下面的手勢導覽列上，蝦皮的按鈕都不會在那裡，bot 也不會點到
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.BOTTOM or Gravity.START
        }
        wm.addView(view, params)
        badge = view
        badgeParams = params
    }

    private fun hideBadge() {
        badge?.let { getSystemService(WindowManager::class.java).removeView(it) }
        badge = null
    }

    private fun keepScreenOn(on: Boolean) {
        val view = badge ?: return
        val params = badgeParams ?: return
        params.flags = if (on) params.flags or WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
        else params.flags and WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON.inv()
        getSystemService(WindowManager::class.java).updateViewLayout(view, params)
    }

    // ---------------- Phone ----------------
    override val ownPackage: String get() = packageName
    override val width: Int get() = resources.displayMetrics.widthPixels
    override val height: Int
        get() = getSystemService(WindowManager::class.java).currentWindowMetrics.bounds.height()

    override fun snapshot(): Snap {
        checkWorker()
        val pkg = foreground()
        if (pkg != SHOPEE) return Snap(emptyList(), pkg, "")
        val roots = ArrayList<AccessibilityNodeInfo>()
        try {
            for (w in windows) {
                try {
                    val root = w.root ?: continue
                    if (root.packageName?.toString() == SHOPEE) roots.add(root) else root.recycle()
                } finally {
                    w.recycle()
                }
            }
        } catch (_: Exception) {
        }
        if (roots.isEmpty()) rootInActiveWindow?.let {
            if (it.packageName?.toString() == SHOPEE) roots.add(it) else it.recycle()
        }
        return try {
            val active = foreground()
            if (active != SHOPEE) Snap(emptyList(), active, "") else Snap.build(roots, active, shopeeWindowClass)
        } finally {
            roots.forEach { it.recycle() }
        }
    }

    // 點擊位置與按壓時間都加抖動：不要每次都落在節點正中央的同一個像素
    override fun tap(p: Pt) {
        val j = Jitter.wobble(p, width, height)
        gesture(Path().apply { moveTo(j.x.toFloat(), j.y.toFloat()) }, Jitter.ms(60, 0.5))
    }

    override fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, ms: Long) {
        val dx = Jitter.int(-12, 12)
        val dy = Jitter.int(-12, 12)
        gesture(Path().apply {
            moveTo((x1 + dx).toFloat(), (y1 + dy).toFloat())
            lineTo((x2 + dx).toFloat(), (y2 + dy).toFloat())
        }, Jitter.ms(ms, 0.3))
    }

    private fun gesture(path: Path, ms: Long) {
        val done = CountDownLatch(1)
        val completed = AtomicBoolean(false)
        val stroke = GestureDescription.StrokeDescription(path, 0, ms)
        val callback = object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) {
                completed.set(true)
                done.countDown()
            }
            override fun onCancelled(g: GestureDescription?) = done.countDown()
        }
        val ok = runActionOnMain { owner ->
            checkControl(owner)
            dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), callback, main)
        }
        check(ok) { "系統未接受手勢" }
        if (!done.await(ms + 2000, TimeUnit.MILLISECONDS) || !completed.get()) {
            throw StopBot("手勢結果不明，停止以避免重複操作")
        }
        checkWorker()
        Thread.sleep(Jitter.ms(80, 0.6))
    }

    override fun back() {
        runActionOnMain { owner ->
            checkControl(owner, allowSystem = true)
            check(performGlobalAction(GLOBAL_ACTION_BACK)) { "返回操作未完成" }
        }
        Thread.sleep(Jitter.ms(100, 0.6))
    }

    private fun checkWorker() {
        checkWorker(Thread.currentThread())
    }

    /** Recheck the worker captured before an action was queued on the main thread. */
    private fun checkWorker(expected: Thread) {
        if (stopping || expected.isInterrupted || worker !== expected) {
            throw InterruptedException("自動操作已停止")
        }
    }

    private fun checkControl(expected: Thread, allowSystem: Boolean = false) {
        checkWorker(expected)
        check(screenOn() && !locked()) { "螢幕已鎖定，暫停操作" }
        val pkg = foreground()
        check(pkg == SHOPEE || (allowSystem && pkg in setOf("android", "com.android.intentresolver"))) {
            "前景已變成 $pkg，暫停操作"
        }
    }

    /**
     * Accessibility actions run on the service main thread.  The worker is
     * checked once before queueing and again immediately before the action so
     * a stop/restart or foreground change cannot leave a stale command queued.
     */
    private fun <T> runActionOnMain(action: (Thread) -> T): T {
        val owner = Thread.currentThread()
        checkWorker(owner)
        val task = FutureTask {
            checkWorker(owner)
            action(owner)
        }
        if (!main.post(task)) throw InterruptedException("無障礙服務已停止")
        try {
            return task.get()
        } catch (e: InterruptedException) {
            task.cancel(false)
            throw e
        } catch (e: ExecutionException) {
            throw (e.cause ?: e)
        }
    }

    override fun foreground(): String {
        val root = rootInActiveWindow ?: return ""
        return try { root.packageName?.toString() ?: "" } finally { root.recycle() }
    }

    override fun screenOn() = getSystemService(PowerManager::class.java).isInteractive

    override fun locked() = getSystemService(KeyguardManager::class.java).isKeyguardLocked

    override fun launchShopee() {
        runActionOnMain { owner ->
            checkWorker(owner)
            check(screenOn() && !locked()) { "螢幕已鎖定，暫停開啟 App" }
            val pkg = foreground()
            check(pkg.isEmpty() || pkg == SHOPEE || pkg == ownPackage || pkg == homePackage()) {
                "前景已變成 $pkg，暫停開啟 App"
            }
            val intent = packageManager.getLaunchIntentForPackage(SHOPEE) ?: run {
                AppLog.w("找不到蝦皮 App")
                return@runActionOnMain
            }
            startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        }
    }

    override fun homePackage(): String =
        packageManager.resolveActivity(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME), 0)
            ?.activityInfo?.packageName ?: ""

    override fun lastToast() = toast

    override fun clearToast() {
        toast = ""
    }

    override fun save(name: String, content: String) {
        val dir = File(filesDir, "captures").apply { mkdirs() }
        File(dir, name).writeText(content)
        dir.listFiles()?.sortedBy { it.name }?.dropLast(100)?.forEach { it.delete() }
    }
}
