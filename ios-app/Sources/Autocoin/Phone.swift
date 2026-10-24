import Foundation
import CoreGraphics
import UIKit

/// 遇到不該繼續的狀況（例如蝦皮回報帳號有風險），整個 Bot 停下來。
struct StopBot: Error { let message: String }

enum BotError: Error {
    case screenshot(String)
    case action(String)
}

/// Bot 需要手機提供的操作。這一版直接在 App 內呼叫 TouchSynthesis 的 XCTest 合成器，
/// 不再走 TCP（電腦版的 ios/tsclient.py 是連手機的 8347 埠）。
///
/// 注意：合成器的 completion 有些會回到主執行緒，所以 Bot 一定要跑在背景執行緒，
/// 不能讓這裡的 semaphore 卡住主執行緒。
final class Phone {
    private let cancelLock = NSLock()
    private var cancelled = false

    func cancel() {
        cancelLock.lock()
        cancelled = true
        cancelLock.unlock()
    }

    private var isCancelled: Bool {
        cancelLock.lock()
        defer { cancelLock.unlock() }
        return cancelled
    }

    private final class Completion<T> {
        private let lock = NSLock()
        private var result: Result<T, Error>?
        let signal = DispatchSemaphore(value: 0)

        func finish(_ value: Result<T, Error>) {
            lock.lock()
            result = value
            lock.unlock()
            signal.signal()
        }

        func value() throws -> T {
            lock.lock()
            let value = result
            lock.unlock()
            guard let value else { throw StopBot(message: "裝置回覆遺失") }
            return try value.get()
        }
    }

    private func perform<T>(_ timeout: Double, _ start: (Completion<T>) -> Void) throws -> T {
        guard !Thread.isMainThread else { throw StopBot(message: "裝置操作必須在背景執行緒執行") }
        guard !isCancelled else { throw StopBot(message: "已停止") }
        let completion = Completion<T>()
        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        start(completion)
        while completion.signal.wait(timeout: .now() + 0.1) == .timedOut {
            guard !isCancelled else { throw StopBot(message: "已停止；已送出的指令結果可能尚未回報") }
            guard ProcessInfo.processInfo.systemUptime < deadline else {
                throw StopBot(message: "裝置操作逾時，結果不明；停止以避免重複操作")
            }
        }
        guard !isCancelled else { throw StopBot(message: "已停止") }
        return try completion.value()
    }
    /// 螢幕尺寸（point）。
    let size: CGSize
    /// 截圖像素 / point（iPhone 11 Pro = 3）。第一次截圖後依實際像素自動校正。
    private(set) var scale: CGFloat = 3
    /// 點擊座標的抖動範圍（px）。
    var tapJitterPx: CGFloat = 9

    init() {
        size = UIScreen.main.bounds.size
    }

    /// 抓一張截圖（JPEG）。
    func shot() throws -> Shot {
        guard size.width == 375 && size.height == 812 else {
            throw StopBot(message: "目前版面座標只支援 375×812 point 的直向畫面")
        }
        let data = try screenshotJPEG()
        guard let image = UIImage(data: data)?.cgImage else {
            throw BotError.screenshot("截圖解碼失敗")
        }
        let s = size.width > 0 ? CGFloat(image.width) / size.width : 3
        guard abs(CGFloat(image.height) / s - size.height) < 1 else {
            throw StopBot(message: "截圖方向或比例不符，停止以避免點錯位置")
        }
        if abs(s - scale) > 0.01 {
            scale = s
            Log.shared.i("螢幕：\(image.width)x\(image.height) px／\(Int(size.width))x\(Int(size.height)) pt，scale=\(s)")
        }
        return Shot(data: data, image: image, scale: scale)
    }

    func tap(_ x: CGFloat, _ y: CGFloat) throws {
        let p = wobble(CGPoint(x: x, y: y))
        let _: Void = try perform(5) { completion in
            TouchSynthesizer.tap(at: p) { error in
                if let error { completion.finish(.failure(StopBot(message: "點擊失敗：\(error)"))) }
                else { completion.finish(.success(Void())) }
            }
        }
    }

    func tap(_ p: CGPoint) throws { try tap(p.x, p.y) }

    func swipe(_ x1: CGFloat, _ y1: CGFloat, _ x2: CGFloat, _ y2: CGFloat, _ duration: Double = 0.4) throws {
        // 每次都滑同一條線、時間一模一樣，是很明顯的機器特徵
        let dx = CGFloat(Jitter.int(-12, 12)), dy = CGFloat(Jitter.int(-12, 12))
        let d = max(0.05, Jitter.ms(duration * 1000, 0.3) / 1000)
        let _: Void = try perform(8) { completion in
            TouchSynthesizer.swipe(from: CGPoint(x: x1 + dx, y: y1 + dy),
                               to: CGPoint(x: x2 + dx, y: y2 + dy),
                               duration: d) { error in
                if let error { completion.finish(.failure(StopBot(message: "滑動失敗：\(error)"))) }
                else { completion.finish(.success(Void())) }
            }
        }
    }

    /// 面板、提領頁左上角的返回鍵（直播間的這個位置是個人頁，別亂按）。
    func back() throws { try tap(BACK.x, BACK.y) }

    /// 把蝦皮叫到前景（沒開就啟動）。
    func activateShopee() throws {
        let _: Void = try perform(20) { completion in
            TouchSynthesizer.activateApplication("com.shopee.tw") { error in
                if let error { completion.finish(.failure(StopBot(message: "叫不到蝦皮：\(error)"))) }
                else { completion.finish(.success(Void())) }
            }
        }
    }

    private func screenshotJPEG() throws -> Data {
        return try perform(10) { completion in
            TouchSynthesizer.takeScreenshot(withQuality: 0.8) { data, error in
                if let data { completion.finish(.success(data)) }
                else { completion.finish(.failure(StopBot(message: "截圖失敗：\(String(describing: error))"))) }
            }
        }
    }

    /// 點在中心附近 ±tapJitterPx 的隨機點，並夾在畫面內。
    private func wobble(_ p: CGPoint) -> CGPoint {
        let r = Int(tapJitterPx)
        return CGPoint(x: min(max(1, p.x + CGFloat(Jitter.int(-r, r))), size.width - 2),
                       y: min(max(1, p.y + CGFloat(Jitter.int(-r, r))), size.height - 2))
    }
}
