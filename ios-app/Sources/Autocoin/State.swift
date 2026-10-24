import Foundation

/// Bot 的設定值（對應 autocoin/config.py，只留 iOS 用得到的）。
struct Settings {
    var withdraw = true              // 自動提領
    var maxRoomMinutes = 75.0        // 保險：單一直播間最多待多久
    var noCoinGrace = 60.0           // 「直播間蝦幣」不見多久就換台
    var watchRoomMinutes = 10.5      // 只為觀看任務待的話，每間待多久
    var panelEveryMinutes = 30.0     // 多久開一次任務獎勵面板
    var withdrawMin = 5.0            // 待提領至少這麼多才提領
    var withdrawDailyLimit = 3       // 蝦皮規定每天最多提領 3 次

    private static let withdrawKey = "settings.withdraw"

    /// 提領開關存 UserDefaults（Android 版存在 SharedPreferences）。
    static func load() -> Settings {
        var s = Settings()
        if UserDefaults.standard.object(forKey: withdrawKey) != nil {
            s.withdraw = UserDefaults.standard.bool(forKey: withdrawKey)
        }
        return s
    }

    func save() {
        UserDefaults.standard.set(withdraw, forKey: Settings.withdrawKey)
    }
}

/// 一間直播間的統計。
final class RoomStat: Codable {
    var claims = 0
    var coins = 0.0
    var done = false
    var wheel = false        // 試過轉盤（iOS 版還沒做，先留著跟電腦版的 JSON 相容）
    var wheelGot = false
    var watched = false

    private enum CodingKeys: String, CodingKey {
        case claims, coins, done, wheel, wheelGot, watched
    }

    init() {}

    /// Older Python/App states omit some room flags; missing keys get defaults,
    /// while explicit nulls or wrong types remain decoding errors.
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        func read<T: Decodable>(_ key: CodingKeys, _ fallback: T) throws -> T {
            if values.contains(key) { return try values.decode(T.self, forKey: key) }
            return fallback
        }
        claims = try read(.claims, 0)
        coins = try read(.coins, 0.0)
        done = try read(.done, false)
        wheel = try read(.wheel, false)
        wheelGot = try read(.wheelGot, false)
        watched = try read(.watched, false)
    }
}

/// 當天的統計（對應 autocoin/state.py 的 Day）。
/// 用 class 而不是 struct：Python 版到處直接改欄位，class 的參考語意才對得起來。
final class Day: Codable {
    var date: String
    var coinClaims = 0              // 直播間蝦幣領取次數（官方上限每天 100）
    var coinTotal = 0.0
    var coinLimitHit = false
    var wheelSpins = 0
    var wheelTotal = 0.0
    var wheelDone = false
    var watchTotal = 0.0            // 在面板領到的觀看任務蝦幣
    var watchMinutes = 0            // 面板上的觀看進度（滿 90 分鐘）
    var watchDone = false
    var withdrawals = 0             // 提領次數（每天 3 次）
    var withdrawalPending = false   // Submit 已送出而結果尚未確認，重啟後也禁止重送。
    var withdrawn = 0.0
    var pending = 0.0               // 最後一次看到的待提領蝦幣數
    var lastPanel = 0.0             // 最後一次開面板的時間（epoch 秒）
    var finalPanel = false          // 今天收尾的面板操作做過了沒
    var rooms: [String: RoomStat] = [:]

    init(date: String) { self.date = date }

    func room(_ key: String) -> RoomStat {
        if let r = rooms[key] { return r }
        let r = RoomStat()
        rooms[key] = r
        return r
    }

    func roomDone(_ key: String) -> Bool { !key.isEmpty && (rooms[key]?.done ?? false) }

    func flag(_ key: String, _ pick: (RoomStat) -> Bool) -> Bool {
        guard let r = rooms[key] else { return false }
        return pick(r)
    }
}

/// 每日統計存成 JSON（欄位名稱跟電腦版的 state-ios.json 一樣，兩邊可以互相對照）。
final class Store {
    private let url: URL
    private var current: Day
    private var persistenceError: String?

    init(_ url: URL) {
        self.url = url
        self.current = Day(date: Store.today())
        do {
            if FileManager.default.fileExists(atPath: url.path) {
                let data = try Data(contentsOf: url)
                // Missing fields from older state versions inherit safe defaults.
                guard var merged = try JSONSerialization.jsonObject(with: Store.encoder().encode(current)) as? [String: Any] else {
                    throw StopBot(message: "無法建立統計檔預設值")
                }
                guard let saved = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let savedDate = saved["date"] as? String,
                      Store.validDate(savedDate) else {
                    throw StopBot(message: "統計檔格式錯誤")
                }
                merged.merge(saved) { _, savedValue in savedValue }
                let loaded = try Store.decoder().decode(Day.self, from: JSONSerialization.data(withJSONObject: merged))
                try Store.validate(loaded)
                // Retain loaded quotas even if the clock is behind the saved day.
                current = loaded
                let today = Store.today()
                guard loaded.date <= today else {
                    throw StopBot(message: "統計日期在未來，請校正時鐘；每日額度不會歸零")
                }
                if loaded.date < today {
                    current = Day(date: today)
                    current.withdrawalPending = loaded.withdrawalPending
                    current.pending = loaded.pending
                }
            }
        } catch {
            persistenceError = "無法讀取統計檔：\(error)"
        }
    }

    static func today() -> String {
        return dateFormatter().string(from: Date())
    }

    private static func dateFormatter() -> DateFormatter {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = TimeZone(secondsFromGMT: 8 * 3600)
        f.dateFormat = "yyyy-MM-dd"
        f.isLenient = false
        return f
    }

    private static func validDate(_ text: String) -> Bool {
        guard text.range(of: #"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"#, options: .regularExpression) != nil,
              !text.hasPrefix("0000-") else { return false }
        let formatter = dateFormatter()
        guard let date = formatter.date(from: text) else { return false }
        return formatter.string(from: date) == text
    }

    private static func validate(_ day: Day) throws {
        guard validDate(day.date), day.coinClaims >= 0, day.wheelSpins >= 0,
              day.withdrawals >= 0, day.watchMinutes >= 0,
              [day.coinTotal, day.wheelTotal, day.watchTotal, day.withdrawn,
               day.pending, day.lastPanel].allSatisfy({ $0.isFinite && $0 >= 0 }),
              day.rooms.values.allSatisfy({ $0.claims >= 0 && $0.coins.isFinite && $0.coins >= 0 }) else {
            throw StopBot(message: "統計檔日期或數值無效")
        }
    }

    static var rewardCalendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 8 * 3600)!
        return calendar
    }

    private static func encoder() -> JSONEncoder {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        e.outputFormatting = [.prettyPrinted, .withoutEscapingSlashes]
        return e
    }

    private static func decoder() -> JSONDecoder {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }

    func checkHealth() throws {
        if persistenceError == nil && current.date > Store.today() {
            persistenceError = "系統日期倒退，請校正時鐘；每日額度不會歸零"
        }
        if let persistenceError { throw StopBot(message: persistenceError) }
    }

    /// 換日時自動重來。
    var day: Day {
        do { try checkHealth() } catch { return current }
        let today = Store.today()
        if current.date < today {
            Log.shared.i("換日了：" + summary())
            let previous = current
            current = Day(date: today)
            current.withdrawalPending = previous.withdrawalPending
            current.pending = previous.pending
            save()
        }
        if current.coinClaims >= 100 { current.coinLimitHit = true }
        if current.wheelSpins >= 10 { current.wheelDone = true }
        return current
    }

    func save() {
        do { try checkHealth() } catch { return }
        do {
            try Store.validate(current)
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            let data = try Store.encoder().encode(current)
            // .atomic handles both first creation and replacement.
            try data.write(to: url, options: .atomic)
        } catch {
            persistenceError = "無法儲存統計檔：\(error)"
            Log.shared.e(persistenceError!)
        }
    }

    func summary() -> String {
        let d = current
        return String(format: "%@ 直播間蝦幣 %d 次 +%.2f｜轉盤 %d 次 +%.2f｜觀看任務 %d 分鐘 +%.2f｜提領 %d 次 %.0f｜待提領 %.2f",
                      d.date, d.coinClaims, d.coinTotal, d.wheelSpins, d.wheelTotal,
                      d.watchMinutes, d.watchTotal, d.withdrawals, d.withdrawn, d.pending)
    }
}
