import Foundation

/// 讓節奏不要那麼像機器人：所有等待與點擊都加一點隨機抖動。
///
/// 為什麼要這樣：風控不只看你裝了什麼，也看「行為的規律性」。固定 10.5 分鐘換台、
/// 每次都點在同一個像素、計時器剩 3 秒準時醒來、每 30 分鐘準時開面板，
/// 這些完美週期性在伺服器端是最好抓的特徵。
/// （跟 android/Jitter.kt 同一套，`up` 用在同一間直播間的停留上限。）
enum Jitter {
    /// 基礎時間乘上 ±pct 的抖動，至少 1ms。
    static func ms(_ base: Double, _ pct: Double = 0.18) -> Double {
        guard base > 0 else { return base }
        return max(1, base * Double.random(in: (1 - pct)...(1 + pct)))
    }

    /// 只往上抖動。用在「至少要等這麼久才做得完」的門檻上，
    /// 例如觀看任務的 10 分鐘 —— 往下抖會讓任務做不完，往上抖只是多待一下。
    static func up(_ base: Double, _ pct: Double = 0.22) -> Double {
        guard base > 0 else { return base }
        return base * Double.random(in: 1.0...(1 + pct))
    }

    /// [min, max] 之間的隨機整數（含頭尾）。
    static func int(_ min: Int, _ max: Int) -> Int { max <= min ? min : Int.random(in: min...max) }

    /// 機率。
    static func chance(_ p: Double) -> Bool { Double.random(in: 0..<1) < p }
}
