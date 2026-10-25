# 安全性、可靠性與效能審查

日期：2026-10-25（Asia/Taipei）。基準版本：`f421c31`。範圍包含 `autocoin/`、`android/`、`ios/`、原有但尚未追蹤的 `ios-jb/` 與 `ios-app/`，以及 GitHub Actions。

## 第三輪完整逐檔複查（2026-10-25，基準 `c5edbec`）

本輪列出並核對全部 74 個基準追蹤檔：71 個 first-party 文字檔完整閱讀，3 個 Gradle 生成／第三方檔案另列供應鏈與二進位限制。每檔結論、缺陷重現、實際命令、JDK／SDK／ASCII 建置路徑與未覆蓋項見 [逐檔審查紀錄](docs/reviews/round3-file-review.md)。沒有以檔名搜尋代替內容審查。

新增修復：

- 5 個 iOS 探索／測試 CLI 與越獄控制器的切前景操作，現在於建立裝置連線前取得共用 `state-ios.json` 排他鎖；鎖衝突無裝置動作，操作例外後會釋放鎖。
- iOS 面板／提領辨識只接受聊天區上方的完整標籤；兩種 Python iOS 版本拒絕負數、小數前綴、溢位金額，以及帶負號或文字前綴的提領面額。
- Android 領取後畫面不明、領取逾時或觀看任務未確認時停止，不回主迴圈重複點擊。越獄版也不再將舊獎勵通知當成新領取成功，領取後重新辨識面板；未知 App 的「稍後再說」不再觸控。
- Swift 實驗來源同步修正上述辨識、面板／提領前重新驗證、拒絕先於確認、未知頁成功提示、跨午夜狀態與收尾喚醒日。主代理複審再補上提領入口：每次點擊前確認最新面板並重新解析座標。

Python／Android 缺陷均有原碼失敗與修正版通過的證據；Android 新觀看測試最初誤用 iOS 標籤，已修 fixture 並在基準 Bot.kt 上重跑出 2 個失敗，才以最終修正版驗證。Swift 沒有編譯器與完整 Apple 工程，**僅靜態審查及 tree-sitter 語法通過，沒有 Swift red/green runtime 證據**。

最終重新驗證：pytest **77**、iOS unittest **47**、ios-jb unittest **55**、Android JVM **35**，合計 **214 項回歸測試通過**；另有 **36 項 loopback 檢查**。Ruff、`pip check`、`pip-audit`、`git diff --check` 通過；Swift 7 檔語法無 error。

Android `testDebugUnitTest lintDebug assembleDebug` 成功，JVM 0 failures／errors／skipped，Lint 0 issue；22 個追蹤 Android 檔案與建置副本 SHA-256 完全一致。APK SHA-256：`9EE47DC91BA3B9EF5810D3F57B78E8D800E0165C66720B2922B563DC25E89D43`。原有 Gradle 10 相容性棄用警告保留。

未連真實手機、未領取或提領、未安裝 APK；沒有本輪效能量測或新倍數宣稱。Swift 的 Apple SDK 型別檢查／連結／執行與外部手機 UI 仍未驗證，詳見逐檔報告的限制。

## 第二輪故障路徑複查（2026-10-25，基準 `bec2151`）

本輪追查停止／重啟、跨程序所有權、TCP 回應完整性、持久化配額與排程邊界。以下修復先新增回歸並觀察原碼失敗，再修改正式路徑；新增 12 個測試，其中 11 個為失敗重現（含參數化共 15 個失敗案例），另 1 個驗證正常啟動仍可清除舊停止旗標，且啟動子程序前已釋放狀態鎖。

| 可重現故障 | 根因與修復 |
|---|---|
| 越獄版沒有成功提示，倒數回到 `05:00` 仍增加領取次數 | 該入口仍保留以倒數重置推定成功的舊分支。現在結果不明即停止，保留畫面、不增加成功統計；已達每日 100 次時不再觸控。 |
| 越獄版收尾從 23:59 跨到 00:01 後再睡一天 | 原來用收尾完成後的日期再加一天。現在以已完成的獎勵日計算醒來時間，收尾跨日即返回新一天的流程。 |
| 面板載入期間已離開面板，或選額後關閉提領／額度用完／面額消失，仍繼續點擊 | 越獄版現在重新辨識等待後的面板；提領送出前重新核對設定、未確認交易、每日額度與可選面額，失配時不寫入新 journal 或送出提領。 |
| ZXTouch 回應含非法 UTF-8 或部分 OCR 行缺少／損壞座標，仍返回其餘文字 | 原來使用替代字元解碼並丟棄壞行，可能同時保留可按按鈕、遺失警示文字。現在拒絕整個回應並關閉連線，後續讀取由既有重連路徑重新取得完整畫面。正常黏包、分段與有效 OCR 路徑仍通過測試。 |
| 控制器重啟後不認得仍在執行的 detached bot，按開始會刪掉該 bot 的停止要求 | 使用真實子程序持有共用狀態鎖重現。控制器清除停止檔前先取得同一狀態鎖；已有 owner 時保留停止檔並拒絕另開子程序。此鎖仍以該統計檔為範圍。 |
| Android 已記錄 100 次領幣或 10 次轉盤，但沒有收到平台上限提示時仍操作 | 讀取當日狀態時由已保存的計數推導上限旗標，並在領取與開轉盤入口再次檢查；從磁碟資料恢復及目前執行中的計數皆受限。 |

修改範圍為 `ios-jb/bot.py`、`ios-jb/serve.py`、`ios-jb/zxtouch.py` 與其測試，以及 Android 的 `Bot.kt`、`Store.kt` 與 JVM 測試。既有三個越獄版觀看任務 fixture 補上真實面板標籤，讓正式畫面辨識可確認所在頁面；原成功金額、未確認提領與不誤記帳的斷言仍保留。

本輪重新執行結果：

| 命令／檢查 | 結果 |
|---|---|
| `.venv/Scripts/python.exe -m pytest -q` | 77 通過 |
| `.venv/Scripts/python.exe -m unittest discover -s ios/tests -q` | 42 通過 |
| `.venv/Scripts/python.exe -m unittest discover -s ios-jb/tests -q` | 50 通過 |
| `.venv/Scripts/python.exe ios-jb/selftest.py` | 36 項 loopback 檢查通過 |
| `gradlew.bat --no-daemon --console=plain testDebugUnitTest lintDebug assembleDebug` | JVM 33 通過，0 failures／errors／skipped；Lint 0 issue；debug APK 建置成功 |
| `python -m ruff check autocoin ios ios-jb tests tools` | 通過 |
| `python -m pip check`、`python -m pip_audit --progress-spinner off` | 相依性一致；當次資料庫未發現已知漏洞 |
| `git diff --check` | 通過 |

共 **202 項回歸測試**，另有 **36 項 loopback 檢查**。Android 使用 JDK 21.0.10、Android 36 SDK 與 Gradle 9.7.1，在既有 ASCII 暫存副本重新同步並建置；22 個 Git 追蹤 Android 檔案與工作目錄 SHA-256 全數一致。APK SHA-256：`5B0BBBE46BB22F6ACA3FE9371EC2B638581F486B9C78DF0CF0158F8259D17EA7`。本輪建置仍顯示 Gradle 10 相容性棄用警告。

已複查的停止 worker 所有權、捕捉原程序的終止行為、損壞狀態不清零、唯讀查詢不覆寫狀態、整體 socket 讀取期限與明確拒絕優先等既有保護，仍由上述回歸涵蓋；沒有為它們額外重排程式。Android 真實服務重建／手勢 callback、外部裝置行為與新版蝦皮畫面仍需實機驗證。本輪未連接真實手機、未領取或提領、未安裝 APK，也未重新執行效能 benchmark；下方先前效能數字保留為歷史證據。

## 接手複查（2026-10-25，基準 `9d2d9db`）

本輪接續未提交的 Python／iOS 修正，並對照原生 Android 的同一套流程。專案由下列獨立入口組成；共用設定與統計不代表各版本的裝置操作實作相同。

| 範圍 | 結構與責任 |
|---|---|
| `autocoin/` | `__main__.py` CLI 與程序鎖 → `bot.py` 排程／狀態機 → `device.py` ADB／uiautomator2；`ui.py` 保存 XML 快照，`screens.py` 解析畫面 |
| `autocoin/state.py` | 各 Python 版本共用的台灣獎勵日、每日額度、原子存檔、未確認提領標記與排他鎖 |
| `ios/` | `bot.py` 控制流程 → `tsclient.py` TCP TouchSynthesis；`screens.py` 與 `ocr.py` 用 Windows OCR、像素與固定幾何辨識畫面 |
| `android/` | `MainActivity` 設定與統計 → `CoinService` 單一 worker／無障礙輸入 → `Bot`；`Ui`／`Screens` 快照解析，`Store` 同步保存狀態 |
| `ios-jb/` | ZXTouch 傳輸、手機端 OCR 與 bot；`serve.py` 是 loopback HTTP 控制器，shell／launchd 負責啟停 |
| `ios-app/` | 實驗性 Swift／Vision 原始碼，缺 Xcode 工程、App 入口與 TouchSynthesizer，無法直接建置 IPA |
| `.github/workflows/` | Checks 驗證 Python／Android，Nightly 在驗證成功後檢查簽署設定，再獨立建置及發布 |

本輪修正：

- Python、iOS 與原生 Android 在送出及回覆階段重新取得當日狀態，確保跨午夜的 journal 與成功計數寫到目前獎勵日；收尾跨日後立即恢復新一天，避免多睡一天。
- 提領選額及送出前重新確認畫面、最新餘額、面額、提領設定與額度；讀不到最新餘額不再沿用舊快照，非有限金額不允許進入自動提領。
- 原生 Android 只接受已知面板／提領頁上的精確餘額差；一般「上限」說明、聊天文字與未知畫面不會解除 journal。「確認提領」最多按一次，明確拒絕優先處理。
- iOS 任務面板與提領畫面切換後停止使用舊座標；未知畫面上的「提領成功」不再當作完成憑據；觸控建立連線失敗亦停止，不重試可能過期的座標。
- TouchSynthesis 的整體期限包含等待 socket 鎖；只有持鎖命令的協定錯誤才關閉連線，排隊逾時不破壞另一個命令。回覆 ID、tree 與 screenshot payload 都在同一 transaction 內驗證。
- 原生 Android 對可信 text／content-desc 建立惰性索引，保留第一個匹配節點並排除自由文字容器；餘額正規表示式只建立一次。此輪沒有執行效能 benchmark，不宣稱新的加速倍數。

接手時 iOS 測試有 3 項失敗：原 fixture 關閉了提領，使新增保護提前返回。測試現明確開啟提領，並使用具有真實畫面邊界的離線 OCR 行，透過正式 parser 驗證提領路徑，未移除保護或弱化成功條件。

本輪驗證：pytest **77** 項、iOS unittest **42** 項、越獄版 unittest **41** 項、Android JVM **30** 項，合計 **190** 項回歸測試通過；另有 loopback selftest **36** 項檢查通過。Ruff、`pip check`、`pip-audit` 與 Git diff whitespace 檢查通過。

Android 在 JDK 21.0.10、Android 36 SDK、Gradle 9.7.1 執行 `testDebugUnitTest lintDebug assembleDebug` 成功，JVM 結果為 0 failures／0 errors／0 skipped，Lint XML 為空問題清單，debug APK 已產生。使用全新 ASCII 暫存建置目錄，逐一比對 22 個 Git 追蹤 Android 檔案的 SHA-256，確認與工作目錄一致。編譯仍有原有 `AccessibilityNodeInfo.recycle()` 棄用及 Gradle 10 相容性警告；未將此次成功宣稱為無警告或 Gradle 10 相容。

以上測試不會接觸真實手機或蝦皮帳戶。下方效能數據、Swift 語法及 workflow 工具結果屬首次稽核，沒有冒充本輪重新量測或真機驗證。

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
