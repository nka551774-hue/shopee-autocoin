# 安全性、可靠性與效能審查

日期：2026-10-25（Asia/Taipei）。基準版本：`f421c31`。範圍包含 `autocoin/`、`android/`、`ios/`、原有但尚未追蹤的 `ios-jb/` 與 `ios-app/`，以及 GitHub Actions。

## 已修正的主要問題

| 影響 | 原本的問題 | 修正 |
|---|---|---|
| 高 | 網路回覆遺失後重送觸控，可能再按領取或提領 | 輸入動作只送一次；傳輸結果不明即停止或保留未確認紀錄，讀取才可重試 |
| 高 | 提領送出後沒有持久化標記；重啟可再次提交 | 送出前先保存 journal，確認成功或明確拒絕才清除；未知結果跨重啟、跨日保留 |
| 高 | 統計檔損壞、時鐘倒退可能把每日配額歸零 | 驗證日期、欄位型別與有限非負數值；失敗封鎖自動操作並保留資料 |
| 高 | Android 停止後立即重啟，舊 worker 仍可能點擊或覆寫新狀態 | worker 結束前不釋放所有權，輸入前再檢查停止、鎖定與前景 App |
| 高 | 越獄版 GET 可啟停，外部網頁可跨站觸發本機控制器 | loopback 綁定、Host／Origin 檢查、CSRF token POST，GET 只讀 |
| 高 | 越獄版 log 可當成 HTML，程序／HTTP worker／日誌無界增長 | log escaping／純文字、CSP、容量上限、程序生命週期鎖及 handler 限制 |
| 高 | TCP 長度、訊息邊界、慢速回應及 ZXTouch 分隔符未驗證 | frame 大小上限、整體 monotonic deadline、緩衝保留黏包餘額、拒絕協定注入 |
| 中 | 倒數重置、按鈕消失或按一下就被計為領取成功 | 成功提示或可核對的餘額變化才記帳；不確定結果不增加成功統計 |
| 中 | 聊天文字／一般「上限」說明可能偽造狀態或清除 journal | Android 排除自由文字容器；提領拒絕限明確文案，確認鍵限「確認提領」 |
| 中 | `--hours` 在內層等待失效；停止後仍執行收尾操作 | 單調時鐘與內層檢查、可取消等待，到期／停止後不再提領 |
| 中 | 午夜唯讀 `status` 可覆寫正在執行的程序所保存的提領 journal | 唯讀查詢不隱式存檔；操作 CLI 使用跨程序鎖，取鎖後重新讀取狀態 |
| 中 | 小按鈕經兩次座標抖動可能點出邊界；未完成觀看被標成完成 | 點擊節點只在有效 bounds 內取點，完整觀看才標記；ADB 滑動依尺寸縮放 |
| 中 | Nightly 先刪所有 release assets、忽略 tag／SDK 錯誤、建置持有寫入 token | 測試通過才建置；驗證簽章／雜湊；獨立發布 job；失敗即停；取消整批刪除 |

上述高／中為本專案內的影響分級，沒有宣稱取得正式 CVE 或完成第三方滲透測試。

## 效能量測

`python tools/benchmark_ui.py` 比較基準版與修改版，使用同一份 1,800 節點 XML，每張快照查詢 90 次，包含 XML 解析與索引建置；每組執行 25 張快照，取 7 組中位數。

Windows、Python 3.11.9 的本次結果：

| 實作 | 每張快照 |
|---|---:|
| 基準 `f421c31` | 132.665 ms |
| 索引版 | 13.198 ms |

此情境約 **10.05 倍**；這是離線畫面解析與查詢量測，不能推論手機操作或領幣速度也提高 10 倍。實際速度仍受 ADB、截圖、OCR、網路與 App 動畫限制。

另有 iOS 像素陣列由 `int16` 改為按需建立的 `uint8`：1125 × 2436 × 3 通道從約 16.4 MB 降為 8.2 MB；尚未需要判色時不配置陣列。OCR bitmap 確實關閉；Android 狀態與日誌 UI 減少重複解析；越獄版快取螢幕資訊並只讀日誌尾端。

## 驗證方式

測試均未操作真實手機、蝦皮帳戶、領取或提領。Python 測試使用假裝置、暫存統計檔、真實子程序鎖及 loopback HTTP／socket。命令見 [README.md](README.md#離線驗證)。

| 檢查 | 本次結果 |
|---|---|
| 電腦版／共用狀態 pytest | 68 項通過 |
| iOS Python unittest | 26 項通過 |
| iOS 越獄版 unittest | 41 項通過 |
| Android JVM regression | 21 項通過，0 failures / 0 skipped |
| 越獄版既有 loopback selftest | 36 項檢查通過 |
| Android assembleDebug / lintDebug | APK 建置成功；Lint 無問題 |
| Ruff / actionlint / Shell 語法 | 通過 |
| pip check / pip-audit | 相依性一致；未發現已知漏洞 |
| Swift | 7 個來源檔語法解析通過，未編譯 |

合計 156 項回歸測試，另有 36 項 selftest 檢查。這些數字不包含真機驗證。

Android 驗證包含真正的 JVM 單元測試、Lint 與 debug APK 建置，使用 Google 官方 SDK 並驗證下載雜湊。因中文工作路徑造成 Gradle worker classpath 問題，使用內容一致的 ASCII 暫存副本建置；未安裝 APK。

GitHub Actions 經 actionlint 驗證；各第三方 action 固定到查證過的 commit SHA。開發環境的 `pip check` 通過，依賴稽核未發現已知漏洞；這只涵蓋當次解析出的套件版本與漏洞資料庫。原本 Python 內建 venv 所帶的舊 pip／setuptools 已在開發環境更新，沒有將工具環境加入版本控制。

## 未確認提領的處理

Python 的 `withdrawal_pending`、Android 的 `withdrawalUncertain`、Swift 的 `withdrawalPending` 表示已提交但未能可靠確認。重啟或跨日不會解除這個限制。

先停止所有 bot，在蝦皮帳戶核對實際餘額與提領紀錄，備份統計資料，再由人根據核對結果更新相應旗標與計數。不要刪掉整份統計檔或在 bot 執行中修改，以免每日額度及其他未確認紀錄一起遺失。若未能確認，保持自動提領停用。

## 尚未解決或未驗證

- 尚未完成真機端到端驗證。新版 Shopee UI、延遲通知、不同手機尺寸與廠牌的 ADB input／無障礙行為仍需實機確認。
- 外部 TouchSynthesis 服務的 TCP 協定仍沒有認證與加密。這個儲存庫只能加強 client 的解析與逾時控制，無法單方替遠端 server 加上認證；不要將它曝露到不受信任的網路。
- OCR 只有影像資訊，無可信前景 App／DOM 證明，無法完整辨識影片或圖片中偽裝的官方提示。未知畫面停止盲點，仍不能取代可信 UI 狀態來源。
- `ios-app/` 尚缺完整 Xcode 專案、App 入口與 `TouchSynthesizer`；Swift 僅做語法解析，沒有 Apple SDK 編譯、型別檢查或真機驗證。
- 排他鎖以統計檔為範圍，不是跨電腦、跨 checkout、跨工具的裝置仲裁。不要讓多個版本同時控制同一台手機。
- 固定名稱的 GitHub release asset 更新不是原子交易；已取消預先刪掉所有資產，但個別檔案替換期間仍可能暫時不可用。
- 未設定簽署 secrets 的 fork 只執行驗證，不產出 signed release；沒有新增、讀取或提交任何私人簽署金鑰。
