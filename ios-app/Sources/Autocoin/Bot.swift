import Foundation
import CoreGraphics
import UIKit

// App 內的提示文字（跟 autocoin/bot.py 同一組，只取比較短的片段，OCR 錯一兩個字也對得到）
private let MSG_RISK = ["不符合領取此獎勵資格", "偵測到風險", "多裝置使用上限", "裝置數限制"]
private let MSG_DAILY_LIMIT = ["超過今天領取的次數", "達領取上限"]
private let MSG_ROOM_DONE = ["已全部發放"]
private let POPUP_DISMISS = ["稍後再說"]  // 蓋住畫面的系統視窗，按這個關掉

private let REASONS: [String: String] = [
    "no_coins": "沒有直播間蝦幣",
    "room_done": "這場的蝦幣發完了",
    "daily_limit": "今天的直播間蝦幣次數用完了",
    "timeout": "待太久了",
    "watched": "觀看任務在這間已累計 10 分鐘",
    "lost": "畫面跑掉了",
]
// 因為這些原因離開的直播間，今天就不再進去
private let DONE_REASONS: Set<String> = ["no_coins", "room_done", "timeout", "watched"]

private let SCAN_ROOMS = 15        // 一輪最多往下滑幾間
private let WATCH_FROZEN = 90.0    // 觀看任務的倒數停住這麼久，代表這間的 10 分鐘已經算滿
private let SWIPE_NEXT = (187.0, 600.0, 187.0, 150.0, 0.25)

private func round2(_ v: Double) -> Double { (v * 100).rounded() / 100 }

/// iOS 版主流程：往上滑換台找有「直播間蝦幣」的直播間、待著領取，定時開面板領觀看任務與提領。
///
/// 跟電腦版（ios/bot.py）同一套流程，差別是：
/// - 畫面全靠 OCR（Vision）＋顏色判斷，讀不到元件樹
/// - 沒有「看更多」列表，只能一間一間滑
/// - 觸控與截圖直接在 App 內呼叫 TouchSynthesis，不走 TCP
/// - 轉盤、紅包雨還沒研究，目前不處理
final class Bot {
    private let cfg: Settings
    private let phone: Phone
    private let store: Store
    private let status: (String) -> Void
    private let captures: URL
    private var end: TimeInterval?  // monotonic uptime，避免校時延長執行。
    private var first = Set<String>()  // 還沒研究過的畫面，每次執行各存一張
    private var stopped = false
    private let stopLock = NSLock()
    private var panelKey: Double = -1  // 面板間隔的抖動只在每個週期抽一次
    private var panelGap: Double = 0

    init(cfg: Settings, phone: Phone, store: Store, status: @escaping (String) -> Void) {
        self.cfg = cfg
        self.phone = phone
        self.store = store
        self.status = status
        let dir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("captures-ios", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        self.captures = dir
    }

    func cancel() {
        stopLock.lock()
        stopped = true
        stopLock.unlock()
        phone.cancel()
    }

    private var isStopped: Bool {
        stopLock.lock()
        defer { stopLock.unlock() }
        return stopped
    }

    private var day: Day { store.day }

    // ---------------- 基本工具 ----------------
    private func ensureRunning() throws {
        try store.checkHealth()
        if isStopped { throw StopBot(message: "已停止") }
        if timeUp() { throw StopBot(message: "設定的執行時間已到") }
    }

    private func snapshot() throws -> Shot {
        try ensureRunning()
        let shot = try phone.shot()
        try checkRisk(shot)
        return shot
    }

    /// 帶抖動的 sleep；被按停止時丟出 StopBot。
    private func pause(_ ms: Double) throws {
        var left = Jitter.ms(ms)
        while left > 0 {
            try ensureRunning()
            let chunk = min(left, 200)
            Thread.sleep(forTimeInterval: chunk / 1000)
            left -= chunk
        }
        try ensureRunning()
    }

    private func look() throws -> (Screen, Shot) {
        var s = try snapshot()
        let scr = detect(s)
        // 系統的評分邀請（「喜歡「蝦皮購物」嗎？」）會蓋住整個畫面，提領完就跳出來過
        if scr != .unknown, let later = s.lines.first(where: { POPUP_DISMISS.contains($0.text) }) {
            Log.shared.i("關掉彈出視窗（\(later.text)）")
            try phone.tap(later.x, later.y)
            try pause(1500)
            s = try snapshot()
        }
        return (detect(s), s)
    }

    private func waitFor(_ wanted: Set<Screen>, timeout: Double = 10) throws -> (Screen, Shot) {
        let deadline = Date().addingTimeInterval(timeout)
        while true {
            let r = try look()
            if wanted.contains(r.0) || Date() >= deadline { return r }
            try pause(700)
        }
    }

    /// 存下目前畫面（截圖＋OCR 文字），之後分析沒看過的狀態用。
    private func capture(_ tag: String, _ s: Shot? = nil) {
        let f = DateFormatter()
        f.dateFormat = "MMdd-HHmmss"
        let safe = tag.replacingOccurrences(of: #"[^\w.-]+"#, with: "_", options: .regularExpression)
        let base = captures.appendingPathComponent("\(f.string(from: Date()))-\(safe)")
        do {
            let shot: Shot
            if let s { shot = s } else { shot = try phone.shot() }
            try shot.data.write(to: base.appendingPathExtension("jpg"))
            let txt = shot.lines
                .map { String(format: "(%4.0f,%4.0f) %@", $0.x, $0.y, $0.text) }
                .joined(separator: "\n")
            try txt.write(to: base.appendingPathExtension("txt"), atomically: true, encoding: .utf8)
        } catch {
            Log.shared.d("存畫面失敗：\(error)")
        }
        pruneCaptures()
        Log.shared.d("已存畫面 \(base.lastPathComponent)")
    }

    private func captureOnce(_ tag: String, _ s: Shot? = nil) {
        if !first.contains(tag) {
            first.insert(tag)
            capture(tag, s)
        }
    }

    private func pruneCaptures() {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: captures, includingPropertiesForKeys: nil)
        else { return }
        let jpgs = files.filter { $0.pathExtension == "jpg" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
        for old in jpgs.dropLast(300) {
            try? fm.removeItem(at: old)
            try? fm.removeItem(at: old.deletingPathExtension().appendingPathExtension("txt"))
        }
    }

    /// 比對 App 的提示文字。遇到風控就停止；回傳 daily_limit / room_done / 空字串。
    private func checkMessages(_ s: Shot) throws -> String {
        let texts = s.texts()
        func hit(_ keys: [String]) -> String? {
            texts.first { t in keys.contains { t.contains($0) } }
        }
        try checkRisk(s)
        if let t = hit(MSG_DAILY_LIMIT) {
            if !day.coinLimitHit {
                Log.shared.i("直播間蝦幣今天的次數用完了（\(t)）")
                day.coinLimitHit = true
                store.save()
            }
            return "daily_limit"
        }
        if hit(MSG_ROOM_DONE) != nil { return "room_done" }
        return ""
    }

    private func checkRisk(_ s: Shot) throws {
        if let text = s.texts().first(where: { t in MSG_RISK.contains { t.contains($0) } }) {
            capture("risk", s)
            throw StopBot(message: "蝦皮回報「\(text)」，為了帳號安全先停止")
        }
    }

    /// 對到今天已經記錄過的直播間就沿用原本的名字。
    private func known(_ name: String) -> String {
        day.rooms.keys.first { same($0, name) } ?? name
    }

    private func done(_ key: String) -> Bool { day.roomDone(key) }

    // ---------------- 導航 ----------------
    /// 收掉蓋在直播間上面的頁面（面板、提領頁）。回不到直播間就回傳 false。
    /// 認不得的畫面只讀取幾次（OCR 偶爾整頁漏讀），仍無法辨識就停止導航。
    /// 沒有可信的前景 App 身分時，不使用固定座標操作未知畫面。
    private func backToRoom(_ scr0: Screen, _ s0: Shot) throws -> Bool {
        var scr = scr0, s = s0
        var unknown = 0
        for _ in 0..<6 {
            if scr == .room { return true }
            if scr == .panel || scr == .withdraw {
                try phone.back()
            } else {
                unknown += 1
                if unknown >= 3 {
                    capture("unknown", s)
                    return false
                }
            }
            try pause(2000)
            let r = try look()
            scr = r.0
            s = r.1
        }
        return scr == .room
    }

    /// 回到直播間。回不去就只等、不亂點，直到有人把畫面切回蝦皮的「直播」分頁。
    private func goRoom() throws -> Shot {
        let first = try look()
        if first.0 == .room { return first.1 }
        if try backToRoom(first.0, first.1) { return try snapshot() }
        Log.shared.w("回不到直播間（畫面已存檔）。不會亂點，請把手機切回蝦皮的「直播」分頁…")
        capture("lost", first.1)
        var scr = first.0
        var s = first.1
        while !timeUp() {
            try pause(20_000)
            let r = try look()
            scr = r.0
            s = r.1
            if scr == .room {
                Log.shared.i("回到直播間，繼續")
                return s
            }
            if scr == .panel || scr == .withdraw {
                _ = try backToRoom(scr, s)
            }
        }
        throw StopBot(message: "時間到了，還是回不到直播間")
    }

    /// 讀目前這間。任務框常常晚一點才出來（或閃一下不見），沒看到直播間蝦幣就再看一次。
    private func readCurrent() throws -> Room {
        var r = read_room(try goRoom())
        if !r.coin.present || r.key.isEmpty {
            try pause(2000)
            let again = read_room(try goRoom())
            if again.coin.present || r.key.isEmpty { r = again }
        }
        return r
    }

    private func nextRoom() throws {
        try phone.swipe(SWIPE_NEXT.0, SWIPE_NEXT.1, SWIPE_NEXT.2, SWIPE_NEXT.3, SWIPE_NEXT.4)
        try pause(3000)
    }

    /// 從目前這間開始往上滑，找今天還沒處理過、有直播間蝦幣的直播間；回傳（目的, 直播主）。
    /// 滑了一輪都沒有的話，觀看任務還沒做完就在最後那間累積觀看時間。
    private func findRoom() throws -> (String, String) {
        let d = day
        var r = Room()
        for i in 0..<SCAN_ROOMS {
            if i > 0 { try nextRoom() }
            r = try readCurrent()
            let key = known(r.key)
            // 沒有任務框的是短影音或沒參加活動的直播；有些直播間只有直播間蝦幣、沒有觀看任務
            if !(r.watch.present || r.coin.present) || done(key) {
                Log.shared.d("略過 \(key.isEmpty ? "?" : key)（\(done(key) ? "處理過了" : "沒有任務框")）")
                continue
            }
            if r.coin.present && !d.coinLimitHit {
                Log.shared.i("→ \(key.isEmpty ? "?" : key) 有直播間蝦幣（\(r.coin.state.isEmpty ? "?" : r.coin.state)）")
                return ("coins", key)
            }
            if d.coinLimitHit && !d.watchDone && !d.flag(key, { $0.watched }) {
                Log.shared.i("→ \(key.isEmpty ? "?" : key)，累積觀看任務")
                return ("watch", key)
            }
        }
        let key = known(r.key)
        if !d.watchDone && r.watch.present && !done(key) && !d.flag(key, { $0.watched }) {
            Log.shared.i("滑了 \(SCAN_ROOMS) 間都沒有直播間蝦幣，在 \(key.isEmpty ? "?" : key) 累積觀看任務")
            return ("watch", key)
        }
        return ("", "")
    }

    // ---------------- 待在直播間 ----------------
    /// 待在目前的直播間直到該走了。purpose：coins / watch。回傳（原因, 直播主）。
    private func stay(_ purpose0: String, _ key0: String) throws -> (String, String) {
        var purpose = purpose0
        var key = key0
        let start = Date()
        var coinMissing: Date?
        var oddState: Date?
        var watchStuck: Date?
        var watchLast: Int?
        var watchSince = Date()
        while true {
            let l = try look()
            if l.0 != .room {
                if !(try backToRoom(l.0, l.1)) { return ("lost", key) }
                continue
            }
            let r = read_room(l.1)
            // 不拿名字判斷有沒有被換台：iOS 只有我們自己滑才會換台，而名字的 OCR 偶爾會整串讀歪
            if key.isEmpty { key = known(r.key) }
            let d = day

            let verdict = try checkMessages(l.1)
            if verdict == "room_done" || (verdict == "daily_limit" && purpose == "coins") {
                return (verdict, key)
            }

            // 觀看任務：每間只算 10 分鐘，算滿之後倒數會停住不動
            let secs = r.watch.seconds
            if secs != watchLast {
                watchLast = secs
                watchSince = Date()
            }
            let watchFull = secs != nil && Date().timeIntervalSince(watchSince) > WATCH_FROZEN
            // 到點會自己領，卡在「領取」太久才幫忙按
            if r.watch.claimable {
                if watchStuck == nil { watchStuck = Date() }
                if let stuck = watchStuck, Date().timeIntervalSince(stuck) > 15 {
                    if let b = r.watch.button { try phone.tap(b) }
                    watchStuck = nil
                }
            } else {
                watchStuck = nil
            }

            if panelDue() {
                try panelTasks()
                continue
            }

            let elapsed = Date().timeIntervalSince(start)
            if elapsed > cfg.maxRoomMinutes * 60 { return ("timeout", key) }
            if purpose == "watch" {
                if r.coin.present && !d.coinLimitHit {
                    Log.shared.i("這間出現直播間蝦幣，改成領蝦幣")
                    purpose = "coins"
                    continue
                }
                if watchFull || elapsed > cfg.watchRoomMinutes * 60 { return ("watched", key) }
                status("累積觀看任務｜\(key.prefix(12))")
                try pause(20_000)
                continue
            }

            // 直播間蝦幣
            if !r.coin.present {
                if coinMissing == nil { coinMissing = Date() }
                if let m = coinMissing, Date().timeIntervalSince(m) > cfg.noCoinGrace {
                    return ("no_coins", key)
                }
                try pause(3000)
                continue
            }
            coinMissing = nil
            if r.coin.claimable {
                if r.claimed.contains("成功") {
                    try pause(1000)   // 上一次的成功提示還在，按鈕還沒換掉
                } else {
                    try claim(r, key)
                }
                continue
            }
            guard let secs2 = r.coin.seconds else {   // 剛領完的空檔、或 OCR 沒讀到
                if oddState == nil { oddState = Date() }
                if let o = oddState, Date().timeIntervalSince(o) > 45 {
                    capture("coin-\(r.coin.state.isEmpty ? "blank" : r.coin.state)", l.1)
                    return ("coin_state:\(r.coin.state.isEmpty ? "?" : r.coin.state)", key)
                }
                try pause(2000)
                continue
            }
            oddState = nil
            status("\(key.prefix(12))｜\(r.coin.state) 後領｜今天 \(d.coinClaims) 次 +\(String(format: "%.2f", d.coinTotal))")
            try pause(min(max(Double(secs2) - 3, 1), 20) * 1000)
        }
    }

    /// 按「直播間蝦幣」的領取，確認有成功。
    private func claim(_ r: Room, _ key: String) throws {
        try ensureRunning()
        guard r.coin.claimable, r.claimed.isEmpty, !day.coinLimitHit, day.coinClaims < 100,
              let button = r.coin.button else { return }
        try phone.tap(button)
        var amount: Double?
        let deadline = Date().addingTimeInterval(6)
        var s = try snapshot()
        while Date() < deadline {
            s = try snapshot()
            let got = read_room(s).claimed
            if got.contains("成功") {
                amount = money(got)
                if amount == nil {
                    Log.shared.w("領取成功但金額沒讀到（\(got)）")
                    amount = 0
                }
                break
            }
            let verdict = try checkMessages(s)
            if !verdict.isEmpty {
                Log.shared.i("領取沒成功：\(REASONS[verdict] ?? verdict)")
                return
            }
            try pause(250)
        }
        if amount == nil {
            capture("claim-unknown", s)
            throw StopBot(message: "直播間領取結果不明，未計入成功；停止以避免重複操作")
        }
        guard let amount else { return }
        let d = day
        let room = d.room(key)
        room.claims += 1
        room.coins = round2(room.coins + amount)
        d.coinClaims += 1
        if d.coinClaims >= 100 { d.coinLimitHit = true }
        d.coinTotal = round2(d.coinTotal + amount)
        store.save()
        Log.shared.i(String(format: "✔ 直播間蝦幣 +%.2f｜%@ 第 %d 次｜今天 %d 次，共 %.2f",
                            amount, key.isEmpty ? "?" : key, room.claims, d.coinClaims, d.coinTotal))
        // 成功提示出來後按鈕還會停在「領取」4 秒左右；等它換掉再走，免得又按一次
        //（那個位置按下去會打開任務獎勵面板）
        for _ in 0..<10 {
            try pause(800)
            if !read_room(try snapshot()).coin.claimable { break }
        }
    }

    // ---------------- 任務獎勵面板 ----------------
    private func panelDue() -> Bool {
        let d = day
        let base = cfg.panelEveryMinutes * 60
        // 門檻每個週期只抽一次，避免「每跑一圈都重抽」而提早觸發
        if panelKey != d.lastPanel {
            panelKey = d.lastPanel
            panelGap = Jitter.ms(base, 0.25)
        }
        if Date().timeIntervalSince1970 - d.lastPanel > panelGap { return true }
        // 觀看任務的蝦幣要當天領，23:40 再開一次
        let now = Date()
        guard let late = Store.rewardCalendar.date(bySettingHour: 23, minute: 40, second: 0, of: now)
        else { return false }
        return now >= late && d.lastPanel < late.timeIntervalSince1970
    }

    /// 開任務獎勵面板：把觀看任務的蝦幣領進待提領，需要時提領。
    /// 直播間會縮成右下角浮窗，倒數不受影響。
    private func panelTasks(final: Bool = false) throws {
        var d = day
        d.lastPanel = Date().timeIntervalSince1970
        store.save()
        var l = try look()
        if l.0 == .room {
            let r = read_room(l.1)
            guard let banner = r.banner else {
                Log.shared.i("這間沒有任務框，晚點再開面板")
                return
            }
            try phone.tap(banner)
            l = try waitFor([.panel], timeout: 15)  // 面板是網頁，有時載得比較慢
        }
        if l.0 != .panel {
            Log.shared.w("任務獎勵面板沒打開（目前在\(l.0.rawValue)）")
            capture("panel-not-open", l.1)
            _ = try backToRoom(l.0, l.1)
            return
        }
        try pause(1500)  // 等畫面載完
        var s = try snapshot()
        guard detect(s) == .panel else { throw StopBot(message: "任務面板已改變，停止使用舊畫面") }
        var p = read_panel(s)
        if let c = p.claim {
            let before = p.pending
            let hadNotice = s.find("已發放至") != nil
            try phone.tap(c)
            try pause(1500)
            s = try snapshot()
            captureOnce("panel-claimed", s)
            let notice = hadNotice ? nil : s.find("已發放至")
            // 領完會跳出「0.21 蝦幣已發放至您的"待提領蝦幣數"」，提示還在的時候按別的都沒反應
            for _ in 0..<8 {
                if s.find("已發放至") == nil { break }
                try pause(1000)
                s = try snapshot()
            }
            guard detect(s) == .panel else { throw StopBot(message: "觀看任務領取後畫面已改變，結果待確認") }
            let after = read_panel(s)
            let increase: Double? = before.flatMap { b in after.pending.map { round2($0 - b) } }
            guard notice != nil || (increase ?? 0) > 0 else {
                capture("panel-claim-unknown", s)
                throw StopBot(message: "觀看任務領取結果不明，未計入成功")
            }
            let amount = (increase ?? 0) > 0 ? increase : notice.flatMap { money($0.text) } ?? p.claimAmount
            d = day
            d.watchTotal = round2(d.watchTotal + (amount ?? 0))
            let shown = amount.map { String(format: "%.2f", $0) } ?? "?"
            Log.shared.i("✔ 觀看任務 +\(shown)（領進待提領）")
            p = after
        }
        // 進度條下有「N分鐘」和終點的「90分鐘」兩個標籤；只剩一個 90 的話分不出是哪個，就不更新
        d = day
        d.lastPanel = Date().timeIntervalSince1970
        if p.minutes.count >= 2 || (p.minutes.first != nil && p.minutes[0] != 90) {
            d.watchMinutes = p.minutes[0]
        }
        if p.done || d.watchMinutes >= 90 { d.watchDone = true }
        if let pending = p.pending { d.pending = pending }
        store.save()
        Log.shared.i(String(format: "待提領 %.2f｜觀看任務 %d/90 分鐘", d.pending, d.watchMinutes))
        if let pending = p.pending, shouldWithdraw(pending, final: final) {
            try withdraw()
        }
        let back = try look()
        _ = try backToRoom(back.0, back.1)
    }

    private func shouldWithdraw(_ pending: Double, final: Bool) -> Bool {
        let d = day
        if !cfg.withdraw || d.withdrawalPending || d.withdrawals >= cfg.withdrawDailyLimit || !pending.isFinite || pending < 1 { return false }
        let now = Date()
        let cal = Store.rewardCalendar
        let late = cal.component(.hour, from: now) == 23 && cal.component(.minute, from: now) >= 40
        return pending >= cfg.withdrawMin || final || late
    }

    /// 在面板按「提領」，選不超過待提領的最大面額，送出並確認待提領有變少。
    private func withdraw() throws {
        var d = day
        guard !d.withdrawalPending else { throw StopBot(message: "先前提領仍待確認，請核對帳戶") }
        guard cfg.withdraw, d.withdrawals < cfg.withdrawDailyLimit else { return }
        var scr = Screen.panel
        var s = try snapshot()
        for _ in 0..<2 {   // 剛關掉的提示有時會吃掉第一下
            guard detect(s) == .panel else { throw StopBot(message: "任務面板已改變，取消開啟提領") }
            guard let withdrawButton = read_panel(s).withdraw else {
                Log.shared.w("目前面板上找不到「提領」")
                return
            }
            try phone.tap(withdrawButton)
            let r = try waitFor([.withdraw], timeout: 6)
            scr = r.0
            s = r.1
            if scr != .panel { break }
        }
        if scr != .withdraw {
            Log.shared.w("提領頁沒打開（目前在\(scr.rawValue)）")
            capture("withdraw-not-open", s)
            return
        }
        try pause(1000)
        s = try snapshot()
        guard detect(s) == .withdraw else { throw StopBot(message: "提領頁已改變，取消選擇金額") }
        var w = read_withdraw(s)
        guard let pending = w.pending, pending.isFinite, pending >= 0 else {
            throw StopBot(message: "無法讀取目前待提領餘額，停止提領")
        }
        let options = w.options.filter { $0.0 > 0 && Double($0.0) <= pending }
        guard let (amount, pos) = options.max(by: { $0.0 < $1.0 }) else {
            Log.shared.i(String(format: "待提領 %.2f，沒有能選的提領金額", pending))
            try phone.back()
            try pause(2000)
            return
        }
        try phone.tap(pos)
        try pause(1000)
        s = try snapshot()
        w = read_withdraw(s)
        guard detect(s) == .withdraw, let latestPending = w.pending, latestPending.isFinite,
              abs(latestPending - pending) <= 0.009, w.options.contains(where: { $0.0 == amount }) else {
            throw StopBot(message: "提領頁、餘額或面額已改變，取消送出")
        }
        guard let submit = w.submit else {
            Log.shared.w("找不到「立即提領」")
            try phone.back()
            try pause(2000)
            return
        }
        captureOnce("withdraw-selected", s)
        d = day
        guard cfg.withdraw, !d.withdrawalPending, d.withdrawals < cfg.withdrawDailyLimit else {
            throw StopBot(message: "目前的提領設定或額度不允許送出")
        }
        d.withdrawalPending = true
        store.save()
        try ensureRunning()
        try phone.tap(submit)
        var done = false
        var confirmed = false
        for i in 0..<10 {
            try pause(1000)
            let r = try look()
            scr = r.0
            s = r.1
            if i == 1 { captureOnce("withdraw-submitted", s) }
            // 每天只能提領 3 次；手動提領過的話程式的計數會少算，看到上限提示就當作今天用完
            // （只看提領頁和面板，直播間的宣傳圖上常有「上限」之類的字）
            if let limit = s.lines.first(where: { ln in
                (scr == .withdraw || scr == .panel) && ln.text.contains("提領")
                    && ["已達", "已超過", "已用完", "不能再"].contains(where: { ln.text.contains($0) })
            })?.text {
                Log.shared.i("提領被拒：\(limit)，今天不再提領")
                d = day
                d.withdrawals = cfg.withdrawDailyLimit
                d.withdrawalPending = false
                store.save()
                done = true
                break
            }
            if !confirmed, scr == .withdraw,
               let confirm = s.lines.first(where: { $0.text == "確認提領" }) {
                try phone.tap(confirm.x, confirm.y)
                confirmed = true
                continue
            }
            var after: Double?
            if scr == .withdraw {
                after = read_withdraw(s).pending
            } else if scr == .panel {
                after = read_panel(s).pending
            }
            if (scr == .withdraw || scr == .panel) && s.find("提領成功") != nil && after == nil {
                after = round2(pending - Double(amount))
            }
            if let a = after, abs(a - (pending - Double(amount))) <= 0.01 {
                d = day
                d.withdrawals += 1
                d.withdrawn += Double(amount)
                d.pending = a
                d.withdrawalPending = false
                store.save()
                Log.shared.i("✔ 提領 \(amount) 蝦幣到「我的蝦幣」（待提領剩 \(String(format: "%.2f", a))，今天第 \(d.withdrawals) 次）")
                done = true
                break
            }
        }
        if !done {
            Log.shared.w("提領結果不明，畫面已存檔")
            capture("withdraw-unknown", s)
            throw StopBot(message: "提領結果不明，已保留待確認標記；停止以避免重複提領")
        }
        if scr == .withdraw {
            try phone.back()
            try pause(2000)
        }
    }

    // ---------------- 主迴圈 ----------------
    func run(hours: Double? = nil) {
        if let hours, !hours.isFinite || hours <= 0 {
            Log.shared.e("執行時間必須為大於零的有限數值")
            return
        }
        end = hours.map { ProcessInfo.processInfo.systemUptime + $0 * 3600 }
        Log.shared.i("開始：" + store.summary())
        var stoppedEarly = false
        while !timeUp() {
            do {
                try cycle()
            } catch let e as StopBot {
                Log.shared.e("停止：\(e.message)")
                stoppedEarly = true
                break
            } catch {
                Log.shared.e("出錯了，10 秒後繼續：\(error)")
                do { try pause(10_000) } catch { stoppedEarly = true; break }
            }
        }
        if stoppedEarly { return }
        Log.shared.i("結束：" + store.summary())
    }

    private func cycle() throws {
        let d = day
        if d.coinLimitHit && d.watchDone {
            try restUntilTomorrow()
            return
        }
        let found = try findRoom()
        if found.0.isEmpty {
            Log.shared.i("滑了 \(SCAN_ROOMS) 間都沒有合適的直播間，1 分鐘後繼續往下找")
            try pause(60_000)
            try nextRoom()
            return
        }
        let result = try stay(found.0, found.1)
        finishRoom(result.1, result.0, found.0)
    }

    private func finishRoom(_ key: String, _ reason: String, _ purpose: String) {
        let d = day
        if !key.isEmpty {
            let room = d.room(key)
            if reason == "watched" { room.watched = true }
            if DONE_REASONS.contains(reason) || reason.hasPrefix("coin_state") { room.done = true }
            store.save()
        }
        let claims = d.rooms[key]?.claims ?? 0
        Log.shared.i("離開 \(key.isEmpty ? "?" : key)：\(REASONS[reason] ?? reason)（這間領了 \(claims) 次）｜\(store.summary())")
    }

    private func restUntilTomorrow() throws {
        var d = day
        let completedDate = d.date
        if !d.finalPanel {
            _ = try goRoom()
            try panelTasks(final: true)
            d = day
            if d.date != completedDate { return }
            d.finalPanel = true
            store.save()
        }
        Log.shared.i("今天能領的都領完了，休息到明天：" + store.summary())
        status("今天領完了，明天再來")
        let cal = Store.rewardCalendar
        guard let start = ISO8601DateFormatter().date(from: completedDate + "T00:03:00+08:00"),
              let wake = cal.date(byAdding: .day, value: 1, to: start) else {
            throw StopBot(message: "無法判斷下一個獎勵日，請校正日期")
        }
        while Date() < wake && !timeUp() {
            try pause(min(300_000, max(1000, wake.timeIntervalSince(Date()) * 1000)))
        }
    }

    private func timeUp() -> Bool {
        guard let end else { return false }
        return ProcessInfo.processInfo.systemUptime >= end
    }
}
