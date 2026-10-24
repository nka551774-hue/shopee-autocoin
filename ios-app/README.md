# Swift 原始碼狀態

此目錄保留 iOS 原生版本的實驗性原始碼。目前沒有 Xcode project、Swift package manifest、App 入口與 `TouchSynthesizer` 實作，不能直接產生可安裝的 IPA。

2026-10-25 審查修正了首次狀態檔寫入、提領紀錄持久化、停止競態、觸控失敗、日誌容量與像素配置等問題。Windows 環境僅完成 Swift 語法解析，未完成 Apple SDK 型別檢查、連結或真機驗證。

現有可執行的 iOS 方案在 [`../ios/`](../ios/) 與 [`../ios-jb/`](../ios-jb/)。完整修正與驗證報告見 [`../AUDIT.md`](../AUDIT.md)。
