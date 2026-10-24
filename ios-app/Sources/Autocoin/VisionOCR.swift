import Foundation
import CoreGraphics
import Vision

/// 一個 OCR 讀到的字（座標是 iOS point，左上原點）。
struct OCRWord {
    let text: String
    let x: CGFloat   // 中心點
    let y: CGFloat
    let w: CGFloat
    let h: CGFloat
}

/// 一行 OCR 文字。Vision 一次回傳一整行，所以 words 通常只有一個；
/// 保留陣列是為了跟電腦版（Windows OCR 會回每個詞）的結構一致。
struct OCRLine {
    let text: String
    let words: [OCRWord]

    var x: CGFloat { words.isEmpty ? 0 : words.reduce(0) { $0 + $1.x } / CGFloat(words.count) }
    var y: CGFloat { words.isEmpty ? 0 : words.reduce(0) { $0 + $1.y } / CGFloat(words.count) }
}

/// 像素層的操作：查顏色比例、只留白字、放大。
enum Pixels {
    struct Bitmap {
        var data: [UInt8]        // RGBA8
        let width: Int
        let height: Int
    }

    /// 把 CGImage 畫進 RGBA8 的緩衝區。
    static func bitmap(_ img: CGImage) -> Bitmap {
        let w = img.width, h = img.height
        var data = [UInt8](repeating: 0, count: w * h * 4)
        data.withUnsafeMutableBytes { buf in
            guard let ctx = CGContext(data: buf.baseAddress, width: w, height: h,
                                      bitsPerComponent: 8, bytesPerRow: w * 4,
                                      space: CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
            else { return }
            ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
        }
        return Bitmap(data: data, width: w, height: h)
    }

    /// 只留「夠亮又接近灰白」的像素，其他塗黑，再反相成深字淺底。
    /// 直播間的任務框是半透明深色底、背後畫面一直在動，原圖直接辨識常常漏字；
    /// 這樣處理後標題與倒數（白字）會留下來，金額的黃字會被濾掉。
    /// （對應電腦版 ocr.py 的 white_text，差別是那邊不需要反相。）
    static func whiteTextMask(_ img: CGImage) -> CGImage? {
        let b = bitmap(img)
        let count = b.width * b.height * 4
        let ptr = UnsafeMutableRawPointer.allocate(byteCount: count, alignment: 1)
        let out = ptr.bindMemory(to: UInt8.self, capacity: count)
        for i in 0..<(b.width * b.height) {
            let r = Int(b.data[i * 4]), g = Int(b.data[i * 4 + 1]), bl = Int(b.data[i * 4 + 2])
            let lum = max(r, max(g, bl)), low = min(r, min(g, bl))
            let white = lum >= 185 && (lum - low) < 90
            let v: UInt8 = white ? 0 : 255
            out[i * 4] = v; out[i * 4 + 1] = v; out[i * 4 + 2] = v; out[i * 4 + 3] = 255
        }
        let provider = CGDataProvider(dataInfo: nil, data: ptr, size: count) { _, p, _ in
            UnsafeMutableRawPointer(mutating: p).deallocate()
        }
        guard let provider else { ptr.deallocate(); return nil }
        return CGImage(width: b.width, height: b.height, bitsPerComponent: 8, bitsPerPixel: 32,
                       bytesPerRow: b.width * 4, space: CGColorSpaceCreateDeviceRGB(),
                       bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
                       provider: provider, decode: nil, shouldInterpolate: false,
                       intent: .defaultIntent)
    }

    /// 放大（OCR 前把小區塊放大兩倍，讀得比較準）。
    static func resize(_ img: CGImage, _ factor: CGFloat) -> CGImage? {
        let w = Int(CGFloat(img.width) * factor), h = Int(CGFloat(img.height) * factor)
        guard w > 0, h > 0,
              let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8,
                                  bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
        else { return nil }
        ctx.interpolationQuality = .high
        ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
        return ctx.makeImage()
    }
}

/// 用 iOS 內建的 Vision（zh-Hant）讀截圖裡的文字與位置。
/// 對應電腦版的 ios/ocr.py（那邊用 Windows.Media.Ocr），差別是：
/// - Vision 的 boundingBox 是「左下原點、0~1 正規化」，要換成左上原點的像素再除回 point
/// - 中文的辨識結果每個字之間會有空格，比對前先拿掉
enum OCR {
    /// 截圖像素 / iOS point（iPhone 11 Pro = 3）。由 Phone 依實際截圖設定。

    /// box 是 iOS point 的 (x1, y1, x2, y2)，只辨識那一塊（比較快也比較準）。
    /// prep 是辨識前的前處理（例如只留白字）。
    static func read(_ image: CGImage, scale: CGFloat = 3, box: CGRect? = nil, upscale: CGFloat = 1,
                     prep: ((CGImage) -> CGImage?)? = nil) -> [OCRLine] {
        var img = image
        var ox: CGFloat = 0, oy: CGFloat = 0
        if let box {
            let r = CGRect(x: box.minX * scale, y: box.minY * scale,
                           width: box.width * scale, height: box.height * scale)
            if let cropped = img.cropping(to: r) {
                img = cropped
                ox = r.minX
                oy = r.minY
            }
        }
        if let prep, let prepped = prep(img) { img = prepped }
        if upscale != 1, let scaled = Pixels.resize(img, upscale) { img = scaled }
        return recognize(img, ox: ox, oy: oy, upscale: upscale, scale: scale)
    }

    private static func recognize(_ img: CGImage, ox: CGFloat, oy: CGFloat,
                                  upscale: CGFloat, scale: CGFloat) -> [OCRLine] {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["zh-Hant"]
        request.usesLanguageCorrection = false
        let handler = VNImageRequestHandler(cgImage: img, options: [:])
        do {
            try handler.perform([request])
        } catch {
            Log.shared.w("OCR 失敗：\(error)")
            return []
        }
        let width = CGFloat(img.width), height = CGFloat(img.height)
        var out: [OCRLine] = []
        for obs in request.results ?? [] {
            guard let text = obs.topCandidates(1).first?.string, !text.isEmpty else { continue }
            let bb = obs.boundingBox
            let px = bb.minX * width
            let py = (1 - bb.maxY) * height          // Vision 是左下原點
            let pw = bb.width * width
            let ph = bb.height * height
            let word = OCRWord(
                text: text,
                x: (ox + (px + pw / 2) / upscale) / scale,
                y: (oy + (py + ph / 2) / upscale) / scale,
                w: pw / upscale / scale,
                h: ph / upscale / scale)
            out.append(OCRLine(text: text.replacingOccurrences(of: " ", with: ""), words: [word]))
        }
        return out
    }
}
