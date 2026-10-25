# Swift 原始碼狀態

此目錄保留 iOS 原生版本的實驗性原始碼。目前沒有 Xcode project、Swift package manifest、App 入口與 `TouchSynthesizer` 實作，不能直接產生可安裝的 IPA。

2026-10-25 審查修正了首次狀態檔寫入、提領紀錄持久化、停止競態、觸控失敗、日誌容量與像素配置等問題。Windows 環境僅完成 Swift 語法解析，未完成 Apple SDK 型別檢查、連結或真機驗證。

第三輪同步修正了聊天標籤與畸形金額辨識、面板／提領送出前重新驗證、拒絕先於確認、跨日使用目前狀態與收尾喚醒日。本機沒有 Swift 編譯器；這些 Swift 修改只有靜態審查及 tree-sitter 語法解析，沒有可執行的 Swift red/green 證據，不能據此宣稱 App 可用。

現有可執行的 iOS 方案在 [`../ios/`](../ios/) 與 [`../ios-jb/`](../ios-jb/)。完整修正與驗證報告見 [`../AUDIT.md`](../AUDIT.md)。
