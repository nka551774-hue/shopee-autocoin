import Foundation

/// App 內的紀錄：最近幾百行留在記憶體給畫面顯示，全部寫進檔案。
/// （對應 android/AppLog.kt）
final class Log {
    static let shared = Log()

    private let fmt: DateFormatter
    private let queue = DispatchQueue(label: "autocoin.log")
    private var lines: [String] = []
    private let file: URL?

    /// 有新的訊息時通知 UI 更新（在主執行緒呼叫）。
    var onChange: (() -> Void)?

    private init() {
        fmt = DateFormatter()
        fmt.dateFormat = "MM-dd HH:mm:ss"
        let dir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first
        file = dir?.appendingPathComponent("autocoin.log")
    }

    func i(_ msg: String) { add("I", msg) }
    func w(_ msg: String) { add("W", msg) }
    func e(_ msg: String) { add("E", msg) }
    func d(_ msg: String) { add("D", msg, show: false) }

    private func add(_ level: String, _ msg: String, show: Bool = true) {
        queue.async {
            let line = "\(self.fmt.string(from: Date())) \(level) \(msg)"
            NSLog("autocoin %@", line)
            if show {
                self.lines.append(line)
                if self.lines.count > 300 { self.lines.removeFirst(self.lines.count - 300) }
            }
            if let f = self.file {
                self.rotate(f)
                let data = Data((line + "\n").utf8)
                if let h = try? FileHandle(forWritingTo: f) {
                    defer { try? h.close() }
                    _ = try? h.seekToEnd()
                    h.write(data)
                } else {
                    try? data.write(to: f)
                }
            }
            if show { DispatchQueue.main.async { self.onChange?() } }
        }
    }

    /// 最近幾行（新到舊由呼叫端決定）。
    func recent(_ n: Int = 200) -> [String] {
        queue.sync { Array(lines.suffix(max(0, n))) }
    }

    private func rotate(_ file: URL) {
        let fm = FileManager.default
        let attrs = try? fm.attributesOfItem(atPath: file.path)
        guard let size = attrs?[.size] as? NSNumber, size.intValue >= 5_000_000 else { return }
        for index in stride(from: 3, through: 1, by: -1) {
            let target = file.appendingPathExtension(String(index))
            let source = index == 1 ? file : file.appendingPathExtension(String(index - 1))
            if fm.fileExists(atPath: target.path) { try? fm.removeItem(at: target) }
            if fm.fileExists(atPath: source.path) { try? fm.moveItem(at: source, to: target) }
        }
    }
}
