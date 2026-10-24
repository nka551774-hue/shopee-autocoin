import Foundation
import CoreGraphics

// 任務框、直播主名稱、留言區的分界（iOS point，iPhone 11 Pro 375x812，跟電腦版 ios/screens.py 一樣）
let TASK_BOX = CGRect(x: 215, y: 120, width: 160, height: 300)
let NAME_BOX = CGRect(x: 40, y: 95, width: 120, height: 30)
let CHAT_TOP: CGFloat = 560
let BACK = CGPoint(x: 19, y: 70)

/// 回傳第一個符合的群組。
func groups(_ text: String, _ pattern: String, anchored: Bool = false) -> [String]? {
    let p = anchored ? "^(?:" + pattern + ")$" : pattern
    guard let re = try? NSRegularExpression(pattern: p) else { return nil }
    let ns = text as NSString
    guard let m = re.firstMatch(in: text, range: NSRange(location: 0, length: ns.length))
    else { return nil }
    return (1..<m.numberOfRanges).map { i in
        let r = m.range(at: i)
        return r.location == NSNotFound ? "" : ns.substring(with: r)
    }
}

/// OCR 常見的誤讀：小數點讀成「·」或「,」、0 讀成 O。只拿來解析數字，不要拿來比對名字。
func clean(_ text: String) -> String {
    text.replacingOccurrences(of: "·", with: ".")
        .replacingOccurrences(of: ",", with: ".")
        .replacingOccurrences(of: "O", with: "0")
        .replacingOccurrences(of: "o", with: "0")
        .replacingOccurrences(of: "：", with: ":")
        .replacingOccurrences(of: " ", with: "")
}

/// 「0·25」→ 0.25。small：金額只有個位數，前面多出來的數字是蝦幣圖示（「30·02」→ 0.02）。
func money(_ text: String, small: Bool = false) -> Double? {
    guard let m = groups(clean(text), #"(\d+)\.(\d{1,2})"#), m.count >= 2 else { return nil }
    let intPart = small ? String(m[0].suffix(1)) : m[0]
    return Double(intPart + "." + m[1])
}

/// 「04:36」→ 276 秒。冒號偶爾會漏掉（「0115」）。
func countdown(_ text: String) -> Int? {
    let t = clean(text)
    if let m = groups(t, #"(\d{1,2}):(\d{2})"#), m.count >= 2, let a = Int(m[0]), let b = Int(m[1]), b < 60 {
        return a * 60 + b
    }
    if let m = groups(t, #"(\d{2})(\d{2})"#, anchored: true), m.count >= 2,
       let a = Int(m[0]), let b = Int(m[1]), b < 60 {
        return a * 60 + b
    }
    return nil
}

/// 直播間是「橘色」的領取／提領按鈕。
func isOrange(_ r: Int, _ g: Int, _ b: Int) -> Bool { r > 200 && g > 50 && g < 140 && b < 90 }

/// 面板上觀看任務的「領取」是黃色。
func isYellow(_ r: Int, _ g: Int, _ b: Int) -> Bool { r > 225 && g > 140 && g < 220 && b < 120 }

/// 直播主名稱的 OCR 偶爾會差一兩個字（「大大的紫」讀成「大大、」），名字夠像就當同一間。
/// 電腦版用 difflib 的 ratio；這裡用 LCS 比例近似，門檻一樣是 0.6。
func same(_ a: String, _ b: String) -> Bool {
    let x = a.lowercased().filter { $0.isLetter || $0.isNumber }
    let y = b.lowercased().filter { $0.isLetter || $0.isNumber }
    if x.isEmpty || y.isEmpty { return false }
    if min(x.count, y.count) >= 2 && (x.contains(y) || y.contains(x)) { return true }
    return similarity(x, y) >= 0.6
}

func similarity(_ a: String, _ b: String) -> Double {
    let xs = Array(a), ys = Array(b)
    if xs.isEmpty && ys.isEmpty { return 1 }
    if xs.isEmpty || ys.isEmpty { return 0 }
    var prev = [Int](repeating: 0, count: ys.count + 1)
    var cur = prev
    for i in 1...xs.count {
        cur[0] = 0
        for j in 1...ys.count {
            cur[j] = xs[i - 1] == ys[j - 1] ? prev[j - 1] + 1 : max(prev[j], cur[j - 1])
        }
        prev = cur
    }
    return 2.0 * Double(prev[ys.count]) / Double(xs.count + ys.count)
}

/// 一張截圖，OCR 結果用到才算、算過就留著。
final class Shot {
    let data: Data                  // JPEG（存檔用）
    let image: CGImage
    let scale: CGFloat
    private lazy var bmp: Pixels.Bitmap = Pixels.bitmap(image)
    private var _lines: [OCRLine]?
    private var _box: [OCRLine]?
    private var _name: String?

    init(data: Data, image: CGImage, scale: CGFloat) {
        self.data = data
        self.image = image
        self.scale = scale
    }

    var lines: [OCRLine] {
        if let l = _lines { return l }
        let l = OCR.read(image, scale: scale)
        _lines = l
        return l
    }

    /// 任務框，只留白字再放大兩倍辨識。
    var box: [OCRLine] {
        if let b = _box { return b }
        let b = OCR.read(image, scale: scale, box: TASK_BOX, upscale: 2, prep: Pixels.whiteTextMask)
        _box = b
        return b
    }

    /// 左上角的直播主名稱。
    var name: String {
        if let n = _name { return n }
        let n = OCR.read(image, scale: scale, box: NAME_BOX, upscale: 2).filter { $0.y < 116 }.map(\.text).joined()
        _name = n
        return n
    }

    func find(_ keys: String..., lines: [OCRLine]? = nil) -> OCRLine? {
        let src = lines ?? self.lines
        return src.first { ln in keys.contains { ln.text.contains($0) } }
    }

    /// 留言區以外的文字（比對 App 的提示用）。
    func texts() -> [String] {
        lines.filter { $0.y < CHAT_TOP }.map(\.text)
    }

    /// box 裡有多少比例的像素是這個顏色。
    func frac(_ box: CGRect, _ color: (Int, Int, Int) -> Bool) -> Double {
        let x1 = max(0, Int(box.minX * scale)), y1 = max(0, Int(box.minY * scale))
        let x2 = min(bmp.width, Int(box.maxX * scale)), y2 = min(bmp.height, Int(box.maxY * scale))
        guard x2 > x1, y2 > y1 else { return 0 }
        var hit = 0
        for y in y1..<y2 {
            var i = (y * bmp.width + x1) * 4
            for _ in x1..<x2 {
                if color(Int(bmp.data[i]), Int(bmp.data[i + 1]), Int(bmp.data[i + 2])) { hit += 1 }
                i += 4
            }
        }
        return Double(hit) / Double((x2 - x1) * (y2 - y1))
    }
}

enum Screen: String {
    case room = "直播間"
    case panel = "任務獎勵面板"
    case withdraw = "提領頁"
    case unknown = "未知"
}

func detect(_ s: Shot) -> Screen {
    if s.find("立即提領", "提領數值") != nil { return .withdraw }
    // 直播間領取成功的提示也有「待提領蝦幣數」（「已發放至「待提領蝦幣數」中」），只認面板上那個標籤
    if s.find("完成任務拿獎勵") != nil || s.lines.contains(where: { $0.text.hasSuffix("待提領蝦幣數") }) {
        return .panel
    }
    // OCR 偶爾會漏掉某一行，多看幾個地方：上方分頁列、直播主名稱下的人數、留言區、任務框
    let tabs = s.lines.filter { $0.y < 90 }
    if s.find("短影音", "推薦", lines: tabs) != nil
        || s.find("觀看者", "說點", "歡迎來到蝦皮直播") != nil
        || s.find("觀看直播", "直播間", lines: s.box) != nil {
        return .room
    }
    return .unknown
}

/// 任務框裡「觀看直播任務」或「直播間蝦幣」其中一格。
struct Task {
    var present = false
    var state = ""                  // OCR 讀到的倒數 mm:ss；按鈕是橘色時固定為「領取」
    var claimable = false
    var button: CGPoint?

    var seconds: Int? { claimable ? nil : countdown(state) }
}

func taskOf(_ s: Shot, _ title: OCRLine?) -> Task {
    guard let title else { return Task() }
    let ty = title.y
    // 「領取」是橘底白字，白字遮罩會把它吃掉，改看顏色；倒數時那格是深色底
    let claimable = s.frac(CGRect(x: 310, y: ty + 14, width: 40, height: 12), isOrange) > 0.4
    let below = s.box.filter { $0.y - ty > 8 && $0.y - ty < 40 && $0.x > 300 }
    return Task(present: true,
                state: claimable ? "領取" : (below.first?.text ?? ""),
                claimable: claimable,
                button: CGPoint(x: 330, y: ty + 20))
}

struct Room {
    var anchor = ""
    var watch = Task()
    var coin = Task()
    var claimed = ""                             // 「成功領取0·25蝦幣」
    var banner: CGPoint?                         // 任務框標題，點了打開任務獎勵面板

    var key: String { anchor }
}

func read_room(_ s: Shot) -> Room {
    let watch = s.find("觀看", lines: s.box)
    let coin = s.find("直播間", "間蝦", lines: s.box)
    let head = s.find("蝦皮直播", lines: s.box)
    let top = watch ?? coin  // 標題在第一格正上方；有些直播間只有直播間蝦幣那一格
    let banner: CGPoint?
    if let head {
        banner = CGPoint(x: head.x, y: head.y)
    } else if let top {
        banner = CGPoint(x: 306, y: top.y - 24)
    } else {
        banner = nil
    }
    let done = s.find("成功領取", lines: s.lines.filter { $0.y < CHAT_TOP })
    return Room(anchor: s.name,
                watch: taskOf(s, watch),
                coin: taskOf(s, coin),
                claimed: done?.text ?? "",
                banner: banner)
}

struct Panel {
    var pending: Double?                // 待提領蝦幣數
    var withdraw: CGPoint?              // 「提領」
    var claim: CGPoint?                 // 觀看任務的「領取」（黃色才算）
    var claimAmount: Double?            // 「領取」上面那個小標籤的金額
    var minutes: [Int] = []
    var done = false                    // 「明天再來」
}

func read_panel(_ s: Shot) -> Panel {
    var p = Panel()
    if let label = s.find("待提領蝦幣數") {
        let amount = s.lines.first { $0.y - label.y > 15 && $0.y - label.y < 45 && $0.x < 200
                                     && money($0.text) != nil }
        p.pending = amount.flatMap { money($0.text) }
        let btn = s.lines.first { $0.text == "提領" && $0.x > 250
                                  && $0.y - label.y > 0 && $0.y - label.y < 60 }
        if let btn {
            p.withdraw = CGPoint(x: btn.x, y: btn.y)
        } else if s.frac(CGRect(x: 285, y: label.y + 19, width: 35, height: 12), isOrange) > 0.3 {
            p.withdraw = CGPoint(x: 301, y: label.y + 25)
        }
    }
    // 「觀看直播任務」是區塊標題，「觀看直播」才是任務本身，按鈕在它右邊
    if let task = s.lines.first(where: { $0.text == "觀看直播" }) {
        if s.frac(CGRect(x: 280, y: task.y + 1, width: 40, height: 12), isYellow) > 0.3 {
            p.claim = CGPoint(x: 300, y: task.y + 6)
        }
        let chip = s.lines.first { $0.x > 270 && $0.y - task.y > -22 && $0.y - task.y < -4 }
        p.claimAmount = chip.flatMap { money($0.text, small: true) }
    }
    // 進度條下有「N分鐘」和終點的「90分鐘」兩個標籤
    p.minutes = s.lines.compactMap { groups($0.text, #"(\d+)分鐘"#, anchored: true) }
        .compactMap { $0.first }.compactMap { Int($0) }.sorted()
    p.done = s.find("明天再來") != nil
    return p
}

struct WithdrawPage {
    var pending: Double?
    var options: [(Int, CGPoint)] = []      // (面額, 位置)
    var submit: CGPoint?                    // 「立即提領」
}

func read_withdraw(_ s: Shot) -> WithdrawPage {
    var w = WithdrawPage()
    let top = s.find("提領數值")
    let bottom = s.find("立即提領")
    guard let top, let bottom, top.y < bottom.y else { return w }
    w.submit = CGPoint(x: bottom.x, y: bottom.y)
    let label = s.find("待提領蝦幣數")
    if let label { w.pending = money(label.text) }
    for ln in s.lines {
        if ln.y <= top.y || ln.y >= bottom.y { continue }
        if let label, ln.text == label.text { continue }
        if let m = groups(clean(ln.text), #"(\d+)\.00$"#), let v = m.first, let amount = Int(v), amount > 0 {
            w.options.append((amount, CGPoint(x: ln.x, y: ln.y)))
        }
    }
    return w
}
