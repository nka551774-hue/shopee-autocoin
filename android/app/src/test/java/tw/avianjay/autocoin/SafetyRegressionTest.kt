package tw.avianjay.autocoin

import android.content.Context
import android.content.ContextWrapper
import android.content.SharedPreferences
import android.graphics.Rect
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.lang.reflect.InvocationTargetException
import java.lang.reflect.Proxy
import java.time.LocalDate
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Regression scenarios use local UI snapshots and memory preferences; no device is connected. */
class SafetyRegressionTest {
    @Test fun countdownRejectsMalformedAndOverflowingInput() {
        assertEquals(125, countdown("02:05"))
        assertEquals(0, countdown("00:00"))
        for (value in listOf("00:60", "1:99", "999999999999999999999999:10", "35791394:08", "1:2", "-1:00")) {
            assertNull(value, countdown(value))
        }
        assertEquals(Int.MAX_VALUE, countdown("35791394:07"))
    }

    @Test fun numericOverflowIsNotAValidReward() {
        assertEquals(0.35, num("成功領取 0.35 Coins")!!, 0.0001)
        assertNull(num("9".repeat(400)))
    }

    @Test fun withdrawalUncertaintySurvivesSerialization() {
        val day = Day("2026-10-25").apply { withdrawalUncertain = true; withdrawals = 2 }
        val restored = Day.fromJson(JSONObject(day.toJson().toString()))
        assertTrue(restored.withdrawalUncertain)
        assertEquals(2, restored.withdrawals)
        assertFalse(Day.fromJson(JSONObject("""{"date":"2026-10-25"}""")).withdrawalUncertain)
    }

    @Test fun corruptedStateStopsAllActionsWithoutOverwritingOrCrashingTheSummary() {
        val ctx = MemoryContext()
        val prefs = ctx.getSharedPreferences("state", Context.MODE_PRIVATE)
        prefs.edit().putString("day", "{broken").commit()
        val store = Store(ctx)
        assertTrue(store.summary().contains("統計資料無效"))
        assertThrows(StopBot::class.java) { store.day }
        assertThrows(StopBot::class.java) { store.save() }
        val phone = FakePhone()
        Bot(phone, store, Settings(ctx)) {}.run()
        assertEquals(0, phone.taps + phone.reads + phone.launches + phone.backs)
        assertEquals("{broken", prefs.getString("day", null))
    }

    @Test fun unresolvedWithdrawalSurvivesRolloverAndNextDayRestart() {
        val ctx = MemoryContext()
        var date = LocalDate.of(2026, 10, 25)
        val store = Store(ctx) { date }
        store.day.apply { withdrawalUncertain = true; pending = 7.5; coinClaims = 99 }
        store.save()
        date = date.plusDays(1)
        assertTrue(store.day.withdrawalUncertain)
        assertEquals(0, store.day.coinClaims)
        assertEquals(7.5, store.day.pending, 0.0)
        val reloaded = Store(ctx) { date.plusDays(1) }
        assertTrue(reloaded.day.withdrawalUncertain)
        assertEquals(7.5, reloaded.day.pending, 0.0)
    }

    @Test fun clockRollbackDuringARunDoesNotResetOrSaveCounters() {
        val ctx = MemoryContext()
        val prefs = ctx.getSharedPreferences("state", Context.MODE_PRIVATE)
        var date = LocalDate.of(2026, 10, 25)
        val store = Store(ctx) { date }
        store.day.apply { coinClaims = 99; withdrawals = 3 }
        store.save()
        val original = prefs.getString("day", null)
        date = date.minusDays(1)
        assertThrows(StopBot::class.java) { store.day }
        assertThrows(StopBot::class.java) { store.save() }
        assertTrue(store.summary().contains("日期倒退"))
        assertEquals(original, prefs.getString("day", null))
    }

    @Test fun restartingWithAnEarlierDateDoesNotResetQuotas() {
        val ctx = MemoryContext()
        val date = LocalDate.of(2026, 10, 25)
        val saved = Store(ctx) { date }
        saved.day.apply { coinClaims = 99; withdrawals = 3 }
        saved.save()
        val prefs = ctx.getSharedPreferences("state", Context.MODE_PRIVATE)
        val original = prefs.getString("day", null)
        val rolledBack = Store(ctx) { date.minusDays(1) }
        assertThrows(StopBot::class.java) { rolledBack.requireReady() }
        assertThrows(StopBot::class.java) { rolledBack.save() }
        assertTrue(rolledBack.summary().contains("系統日期早於"))
        assertEquals(original, prefs.getString("day", null))
    }

    @Test fun invalidBooleanTypesCannotUnlockTransactionOrLimits() {
        for (key in listOf("withdrawalUncertain", "coinLimitHit", "wheelDone", "watchDone", "finalPanel")) {
            for (bad in listOf<Any>("false", "true", 0, 1, JSONObject.NULL)) {
                val json = Day("2026-10-25").toJson().put(key, bad)
                assertThrows("$key=$bad", IllegalArgumentException::class.java) { Day.fromJson(json) }
            }
        }
        val ctx = MemoryContext()
        val prefs = ctx.getSharedPreferences("state", Context.MODE_PRIVATE)
        val invalid = Day("2026-10-25").toJson().put("withdrawalUncertain", "false").toString()
        prefs.edit().putString("day", invalid).commit()
        val store = Store(ctx) { LocalDate.of(2026, 10, 26) }
        assertThrows(StopBot::class.java) { store.day }
        assertThrows(StopBot::class.java) { store.save() }
        assertEquals(invalid, prefs.getString("day", null))
    }

    @Test fun malformedRoomFlagsAndContainersAreRejected() {
        val original = Day("2026-10-25").apply { room("a").wheel = true }.toJson()
        original.getJSONObject("rooms").getJSONObject("a").put("wheel", "false")
        assertThrows(IllegalArgumentException::class.java) { Day.fromJson(original) }
        assertThrows(org.json.JSONException::class.java) { Day.fromJson(original.put("rooms", "broken")) }
    }

    @Test fun failedPersistenceStopsAutomation() {
        val ctx = MemoryContext()
        val store = Store(ctx)
        ctx.writesSucceed = false
        assertThrows(StopBot::class.java) { store.save() }
    }

    @Test fun invalidStoredCountersAndAmountsAreRejected() {
        assertThrows(IllegalArgumentException::class.java) {
            Day.fromJson(JSONObject("""{"date":"2026-10-25","withdrawals":-1}"""))
        }
        assertThrows(IllegalArgumentException::class.java) {
            Day.fromJson(JSONObject("""{"date":"2026-10-25","pending":"NaN"}"""))
        }
    }

    @Test fun userChatIsExcludedButSystemRiskTextStillStops() {
        val ctx = MemoryContext()
        val phone = FakePhone()
        val bot = Bot(phone, Store(ctx), Settings(ctx)) {}
        val chat = node("public_screen_view", "", 0).apply { end = 2 }
        val fakeMessage = node("", "偵測到風險活動", 1)
        val safe = Snap(listOf(chat, fakeMessage), SHOPEE, "")
        invoke(bot, "checkRisk", safe, "")
        assertTrue(safe.textsExcluding(setOf("public_screen_view")).isEmpty())
        val real = Snap(listOf(node("system_notice", "偵測到風險活動", 0)), SHOPEE, "")
        assertThrows(StopBot::class.java) { invoke(bot, "checkRisk", real, "") }
    }

    @Test fun everySnapshotChecksRiskBeforeTheCallerCanAct() {
        val ctx = MemoryContext()
        val phone = FakePhone().apply {
            screen = Snap(listOf(node("", "待提領蝦幣數", 0), node("", "不符合領取此獎勵資格", 1)), SHOPEE, "")
        }
        val bot = Bot(phone, Store(ctx), Settings(ctx)) {}
        assertThrows(StopBot::class.java) { invoke(bot, "snapshot") }
        assertEquals(0, phone.taps)
    }

    @Test fun missingClaimButtonCannotProduceSuccess() {
        val ctx = MemoryContext()
        val store = Store(ctx)
        val phone = FakePhone().apply {
            screen = Snap(listOf(node("tv_coin_claimed_content", "成功領取 9 Coins", 0)), SHOPEE, "")
        }
        val bot = Bot(phone, store, Settings(ctx)) {}
        invoke(bot, "claim", readRoom(Snap(emptyList(), SHOPEE, "")))
        assertEquals(0, phone.taps)
        assertEquals(0, store.day.coinClaims)
    }

    @Test fun unknownWithdrawalBlocksEvenFinalAutomaticWithdrawal() {
        val ctx = MemoryContext()
        val store = Store(ctx)
        store.day.withdrawalUncertain = true
        val bot = Bot(FakePhone(), store, Settings(ctx)) {}
        assertEquals(false, invoke(bot, "shouldWithdraw", 100.0, true))
    }

    @Test fun withdrawalMarkerIsDurableBeforeSubmitCanBeInterrupted() {
        val ctx = MemoryContext()
        val store = Store(ctx)
        val phone = FakePhone().apply {
            screen = Snap(listOf(node("", "立即提領", 0), node("", "待提領蝦幣數:5.00", 1), node("", "5.00", 2)), SHOPEE, "")
            interruptAtTap = 3 // Open page, choose denomination, submit.
        }
        val bot = Bot(phone, store, Settings(ctx)) {}
        val panel = Snap(listOf(node("", "提領", 0)), SHOPEE, "")
        assertThrows(InterruptedException::class.java) { invoke(bot, "withdraw", panel) }
        assertEquals(3, phone.taps)
        assertTrue(Store(ctx).day.withdrawalUncertain)
        assertEquals(0, Store(ctx).day.withdrawals)
    }

    @Test fun leavingWatchEarlyDoesNotMarkItComplete() {
        val ctx = MemoryContext()
        val store = Store(ctx)
        val bot = Bot(FakePhone(), store, Settings(ctx)) {}
        invoke(bot, "finishRoom", setOf("room"), "lost", "watch")
        assertFalse(store.day.room("room").watched)
        assertFalse(store.day.room("room").done)
        invoke(bot, "finishRoom", setOf("room"), "watched", "watch")
        assertTrue(store.day.room("room").watched)
        assertTrue(store.day.room("room").done)
    }

    @Test fun fastStopStartCannotReplaceAWorkerStillExiting() {
        val service = CoinService()
        val started = CountDownLatch(1)
        val release = CountDownLatch(1)
        val oldWorker = Thread {
            started.countDown()
            while (release.count > 0) {
                try { release.await() } catch (_: InterruptedException) { /* Simulate slow cleanup. */ }
            }
        }
        val workerField = CoinService::class.java.getDeclaredField("worker").apply { isAccessible = true }
        workerField.set(service, oldWorker)
        oldWorker.start()
        try {
            assertTrue(started.await(2, TimeUnit.SECONDS))
            service.stopBot()
            service.startBot()
            assertSame(oldWorker, workerField.get(service))
            assertTrue(service.running)
            assertEquals("正在停止", service.statusText)
        } finally {
            release.countDown()
            oldWorker.join(2000)
        }
        assertFalse(oldWorker.isAlive)
    }

    @Test fun cancelledOrUnownedWorkerCannotPassTheActionGuard() {
        val service = CoinService()
        assertThrows(InterruptedException::class.java) { invoke(service, "checkWorker") }
        CoinService::class.java.getDeclaredField("worker").apply { isAccessible = true }.set(service, Thread.currentThread())
        invoke(service, "checkWorker")
        CoinService::class.java.getDeclaredField("stopping").apply { isAccessible = true }.setBoolean(service, true)
        assertThrows(InterruptedException::class.java) { invoke(service, "checkWorker") }
    }

    @Test fun foreignForegroundOverridesShopeeLookingNodes() {
        val snap = Snap(listOf(node("tv_anchor_name", "room", 0)), "com.other.app", "")
        assertEquals(Screen.OTHER_APP, detect(snap))
    }

    @Test fun idIndexPreservesFirstMatchingNode() {
        val first = node("same", "first", 0)
        val snap = Snap(listOf(first, node("same", "second", 1)), SHOPEE, "")
        assertSame(first, snap.id("same"))
    }

    @Test fun trustedTextIndexIgnoresChatControlsAndKeepsFirstRealMatch() {
        val chat = node("public_screen_view", "", 0).apply { end = 3 }
        val fakeSubmit = node("", "立即提領", 1)
        val fakeBalance = node("", "待提領蝦幣數:50", 2)
        val safe = Snap(listOf(chat, fakeSubmit, fakeBalance), SHOPEE, "")
        assertNull(safe.text("立即提領"))
        assertEquals(Screen.UNKNOWN, detect(safe))
        val ctx = MemoryContext()
        val bot = Bot(FakePhone(), Store(ctx), Settings(ctx)) {}
        assertNull(invoke(bot, "pendingAmount", safe))
        assertNull(invoke(bot, "pendingAmount", Snap(listOf(node("", "待提領蝦幣數:-5", 0)), SHOPEE, "")))
        val first = node("", "立即提領", 3)
        val both = Snap(listOf(chat, fakeSubmit, fakeBalance, first, node("", "立即提領", 4)), SHOPEE, "")
        assertSame(first, both.text("立即提領"))
        assertSame(first, both.text("立即提領"))
    }

    @Test fun nonfiniteBalanceCannotRequestWithdrawal() {
        val ctx = MemoryContext()
        val bot = Bot(FakePhone(), Store(ctx), Settings(ctx)) {}
        for (value in listOf(Double.NaN, Double.POSITIVE_INFINITY, Double.NEGATIVE_INFINITY)) {
            assertEquals(false, invoke(bot, "shouldWithdraw", value, true))
        }
    }

    @Test fun missingLatestBalanceCannotFallBackToEarlierPage() {
        val ctx = MemoryContext()
        val store = Store(ctx)
        val phone = FakePhone().apply { screens.addAll(listOf(withdrawPage(), withdrawPage(null))) }
        invoke(Bot(phone, store, Settings(ctx), sleepFor = {}) {}, "withdraw", withdrawPanel())
        assertEquals(1, phone.taps)
        assertFalse(store.day.withdrawalUncertain)
    }

    @Test fun changedBalanceOrDenominationBlocksSubmit() {
        for (changed in listOf(withdrawPage(15.0), withdrawPage(null), withdrawPage(10.0, enabled = false))) {
            val ctx = MemoryContext()
            val store = Store(ctx)
            val phone = FakePhone().apply { screens.addAll(listOf(withdrawPage(), withdrawPage(), changed)) }
            val bot = Bot(phone, store, Settings(ctx), sleepFor = {}) {}
            assertThrows(StopBot::class.java) { invoke(bot, "withdraw", withdrawPanel()) }
            assertEquals(2, phone.taps)
            assertFalse(store.day.withdrawalUncertain)
        }
    }

    @Test fun changedLimitOrSettingBlocksSubmit() {
        for (disable in listOf(false, true)) {
            val ctx = MemoryContext()
            val store = Store(ctx)
            val cfg = Settings(ctx)
            val phone = FakePhone().apply {
                screen = withdrawPage()
                onTap = { if (it == 2) { if (disable) cfg.withdraw = false else store.day.withdrawals = 3 } }
            }
            assertThrows(StopBot::class.java) {
                invoke(Bot(phone, store, cfg, sleepFor = {}) {}, "withdraw", withdrawPanel())
            }
            assertEquals(2, phone.taps)
            assertFalse(store.day.withdrawalUncertain)
        }
    }

    @Test fun untrustedOrInexactResultCannotClearWithdrawalJournal() {
        val unknown = Snap(listOf(node("", "待提領蝦幣數:5.00", 0)), SHOPEE, "")
        for (after in listOf(unknown, withdrawPage(0.0), withdrawPage(extra = listOf("每日提領上限三次", "確認")))) {
            val ctx = MemoryContext()
            val store = Store(ctx)
            val phone = FakePhone().apply { screens.addAll(listOf(withdrawPage(), withdrawPage(), withdrawPage(), after)) }
            invoke(Bot(phone, store, Settings(ctx), sleepFor = {}) {}, "withdraw", withdrawPanel())
            assertTrue(Store(ctx).day.withdrawalUncertain)
            assertEquals(0, store.day.withdrawals)
            assertEquals(3, phone.taps)
            if (after === unknown) assertEquals(0, phone.backs)
        }
    }

    @Test fun explicitConfirmationIsOnlySentOnce() {
        val ctx = MemoryContext()
        val store = Store(ctx)
        val phone = FakePhone().apply {
            screens.addAll(listOf(withdrawPage(), withdrawPage(), withdrawPage(), withdrawPage(extra = listOf("確認提領"))))
        }
        invoke(Bot(phone, store, Settings(ctx), sleepFor = {}) {}, "withdraw", withdrawPanel())
        assertEquals(4, phone.taps)
        assertTrue(Store(ctx).day.withdrawalUncertain)
    }

    @Test fun rejectionWinsOverConfirmationButton() {
        val ctx = MemoryContext()
        val store = Store(ctx)
        val phone = FakePhone().apply {
            screens.addAll(listOf(withdrawPage(), withdrawPage(), withdrawPage(),
                withdrawPage(extra = listOf("確認提領", "已達每日提領上限"))))
        }
        invoke(Bot(phone, store, Settings(ctx), sleepFor = {}) {}, "withdraw", withdrawPanel())
        assertEquals(3, phone.taps)
        assertEquals(3, store.day.withdrawals)
        assertFalse(Store(ctx).day.withdrawalUncertain)
    }

    @Test fun withdrawalCrossingMidnightPersistsAndUpdatesLiveDay() {
        for (rolloverAt in listOf(2, 3)) {
            val ctx = MemoryContext()
            var date = LocalDate.of(2026, 10, 25)
            val store = Store(ctx) { date }
            val phone = FakePhone().apply {
                screens.addAll(listOf(withdrawPage(), withdrawPage(), withdrawPage(), withdrawPage(5.0)))
                onTap = { tap ->
                    if (tap == 3) assertTrue(Store(ctx) { date }.day.withdrawalUncertain)
                    if (tap == rolloverAt) { date = date.plusDays(1); store.day }
                }
            }
            invoke(Bot(phone, store, Settings(ctx), sleepFor = {}) {}, "withdraw", withdrawPanel())
            val saved = Store(ctx) { date }.day
            assertEquals("2026-10-26", saved.date)
            assertEquals(1, saved.withdrawals)
            assertEquals(5.0, saved.withdrawn, 0.0)
            assertFalse(saved.withdrawalUncertain)
        }
    }

    private fun withdrawPanel() = Snap(listOf(node("", "提領", 0)), SHOPEE, "")

    private fun withdrawPage(pending: Double? = 10.0, extra: List<String> = emptyList(), enabled: Boolean = true): Snap {
        val nodes = mutableListOf(node("", "立即提領", 0))
        if (pending != null) nodes.add(node("", "待提領蝦幣數:$pending", nodes.size))
        nodes.add(N("", "5.00", "", Rect(), false, nodes.size, enabled))
        for (text in extra) nodes.add(node("", text, nodes.size))
        return Snap(nodes, SHOPEE, "")
    }

    private fun node(rid: String, text: String, index: Int) = N(rid, text, "", Rect(), false, index)

    private fun invoke(target: Any, name: String, vararg args: Any): Any? {
        val method = target.javaClass.declaredMethods.single { it.name == name && it.parameterCount == args.size }
        method.isAccessible = true
        return try { method.invoke(target, *args) } catch (e: InvocationTargetException) { throw e.cause!! }
    }

    private class FakePhone : Phone {
        override val ownPackage = "tw.avianjay.autocoin"
        override val width = 1080
        override val height = 2400
        var screen = Snap(emptyList(), SHOPEE, "")
        var taps = 0
        var reads = 0
        var launches = 0
        var backs = 0
        var interruptAtTap = -1
        val screens = ArrayDeque<Snap>()
        var onTap: (Int) -> Unit = {}
        override fun snapshot(): Snap { reads++; if (screens.isNotEmpty()) screen = screens.removeFirst(); return screen }
        override fun tap(p: Pt) {
            if (++taps == interruptAtTap) throw InterruptedException("test cancellation")
            onTap(taps)
        }
        override fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, ms: Long) {}
        override fun back() { backs++ }
        override fun foreground() = screen.pkg
        override fun screenOn() = true
        override fun locked() = false
        override fun launchShopee() { launches++ }
        override fun homePackage() = "launcher"
        override fun lastToast() = ""
        override fun clearToast() {}
        override fun save(name: String, content: String) {}
    }

    private class MemoryContext : ContextWrapper(null) {
        var writesSucceed = true
        private val preferences = HashMap<String, SharedPreferences>()
        override fun getSharedPreferences(name: String, mode: Int): SharedPreferences = preferences.getOrPut(name) {
            val data = HashMap<String, Any?>()
            lateinit var editor: SharedPreferences.Editor
            editor = Proxy.newProxyInstance(javaClass.classLoader, arrayOf(SharedPreferences.Editor::class.java)) { _, method, args ->
                when {
                    method.name.startsWith("put") -> { data[args[0] as String] = args[1]; editor }
                    method.name == "commit" -> writesSucceed
                    method.name == "apply" -> null
                    method.name == "remove" -> { data.remove(args[0]); editor }
                    method.name == "clear" -> { data.clear(); editor }
                    else -> null
                }
            } as SharedPreferences.Editor
            Proxy.newProxyInstance(javaClass.classLoader, arrayOf(SharedPreferences::class.java)) { _, method, args ->
                when {
                    method.name == "edit" -> editor
                    method.name == "getAll" -> data.toMap()
                    method.name == "contains" -> data.containsKey(args[0])
                    method.name.startsWith("get") -> data[args[0]] ?: args[1]
                    else -> null
                }
            } as SharedPreferences
        }
    }
}
