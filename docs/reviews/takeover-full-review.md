# 接手全庫逐檔審查

日期：2026-10-25～2026-10-26（Asia/Taipei）。分支：`main`。審查起點：`b8489efa4f1e851c6fe7cd05730f2a59edf47603`。

## 範圍與方法

- 先以 `git ls-files` 固定 75 個基準追蹤檔，完整閱讀所有第一方原始碼、測試、設定、CI、腳本及文件；長檔分段讀到 EOF，沒有以搜尋命中或片段取代全文審查。
- 最終以 `git ls-files --cached --others --exclude-standard` 重建清單；加入本文件後共 76 個路徑。下方 ledger 包含 75 個基準檔與 ledger 自身，另以機器比對確認零漏項。
- `android/gradle/wrapper/gradle-wrapper.jar` 依二進位類型核對：47,505 bytes、SHA-256 `7A9CE74CFF467CA1BF60A4FCD9F05185ACCEDA4D0F382434D393E17864262C5D`、ZIP CRC 全通過、34 個 entry 全部可讀並逐 entry 計算 SHA-256；manifest 為 Gradle Wrapper／Apache-2.0。wrapper properties 的 HTTPS URL、distribution SHA-256、timeout 與驗證開關均已核對，並實際執行 Gradle 9.7.1。
- `gradlew`／`gradlew.bat` 為 Gradle 生成腳本，但本輪仍完整讀到 EOF、檢查行尾／shell 語法，並由 Windows 實際建置覆蓋。需求清單與 Gradle 設定逐行核對，repository 沒有其他 lockfile 或二進位。
- 所有動態驗證均使用假裝置、暫存狀態、loopback socket/HTTP 或純英文 Android 副本；沒有連手機、登入帳戶、領取、提領、安裝或發布。

## 確認並修正的缺陷

| 影響 | 原因 | 修正與 red→green 證據 |
|---|---|---|
| 高 | 電腦版、iOS 與 ios-jb 的 `run()` 吞掉風控、前景變更或狀態損壞等 fatal 例外，CLI 可能以 0 結束；iOS `main()` 又把 `StopBot` 轉成正常返回。 | 只有已到期／停止檔屬正常結束，其他 fatal 一律傳到 CLI 邊界；iOS 回傳 exit 1。原碼桌面 3 failures、iOS 2 failures，修後回歸全過。 |
| 高 | iOS 與 ios-jb 保存風險畫面後，清理被 Windows／iOS 鎖住的舊檔會拋 `OSError`，遮蔽原本應停止帳戶操作的 `StopBot`。 | 診斷清理改成逐檔 best-effort，列舉／刪除失敗只寫 debug；兩個原碼 error 均由鎖檔回歸重現並轉綠。 |
| 高 | Android 在 worker thread 檢查 stop／前景後才呼叫無障礙動作，排隊或切換期間可發生 TOCTOU；gesture callback 的取消／逾時又被當成一般可重試錯誤。 | 手勢、返回與啟動 App 改在 service main thread 排隊，排隊前及實際執行前以 captured worker 世代重驗 stop、interrupt、鎖定及允許的前景；取消／逾時視為結果不明並 fatal 停止。保留 fork 原本從空前景、自己的 App 或 launcher 自動叫回蝦皮的產品行為。原碼 worker-generation 回歸失敗、修後通過。 |
| 中 | Android 廣告關閉鈕高度誤用水平 `x()` 比例，非基準長寬比可能漏掉真正關閉鈕。 | 將寬、高拆成獨立軸縮放的 pure helper；原碼垂直尺度回歸失敗、修後通過。 |
| 中 | ios-jb loopback 控制器只取第一個 `Content-Length`；同一請求帶兩個互相矛盾的長度仍可通過 token 並觸發 `/start`。 | POST 現要求恰好一個 `Content-Length`。原碼 raw HTTP 測試實際得到 `200 OK` 且呼叫 start；修後回 `400`、零動作。 |

審查時另追查 Swift `Store.save()` 不拋錯的候選：安全關鍵的提領 journal 在送出前緊接 `ensureRunning()`／`checkHealth()`，寫入失敗會在觸控前停止，因此本輪沒有為此修改實驗性 Swift 來源。上游唯一暫停行為提交未直接合併；本輪只移植真正的 action race 修正，保留使用者 fork 自動回到蝦皮的行為。

## 驗證結果

原始輸出均保存在 `logs/takeover-20261025/`（受 `.gitignore` 排除，不提交）。每個 final gate 均記錄 `exit_code=0`。

| Gate | 結果 | Log |
|---|---:|---|
| pytest | 80 passed | `final-pytest.log` |
| iOS unittest | 50 passed | `final-ios-unittest.log` |
| ios-jb unittest | 58 passed | `final-ios-jb-unittest.log` |
| ios-jb loopback selftest | 36 checks passed | `final-ios-jb-selftest.log` |
| Android JVM | 37 tests，0 failures/errors/skipped | `final-android.log`、`final-android-artifacts.log` |
| Android Lint / debug APK | 0 issues；`assembleDebug` 成功 | `final-android.log`、`final-android-artifacts.log` |
| Android 來源副本 | 建置前後 22/22 SHA-256 相符 | `final-android-hashes.log` |
| debug APK | v2 簽章驗證通過；SHA-256 `67FD9DACE917C40DAB2143308301A7F9EF3B93DE3E49849E9600F75F7300A204` | `final-apksigner.log`、`final-android-artifacts.log` |
| Ruff / compileall | 通過 | `final-ruff.log`、`final-compileall.log` |
| pip check / pip-audit | 相依一致；當次資料庫無已知漏洞 | `final-pip-check.log`、`final-pip-audit.log` |
| actionlint 1.7.7 | 2 workflows 通過 | `final-actionlint.log` |
| XML / YAML | 5 XML、2 YAML 解析通過 | `final-xml.log`、`final-yaml.log` |
| shell syntax | Git Bash `bash -n` 通過 3 個腳本 | `final-shell-syntax.log` |
| Swift tree-sitter | 7 個來源檔語法無 error；僅語法 | `final-swift-syntax.log` |
| Gradle wrapper | JAR 34 entries 完整、Gradle 9.7.1 可執行 | `final-wrapper-audit.log`、`final-gradle-version.log` |
| diff / ledger | whitespace 與 76/76 路徑比對通過 | `final-git-diff-check.log`、`final-ledger-check.log` |

可執行回歸合計 225 項（80 + 50 + 58 + 37），另有 36 項 loopback selftest。原碼 red 證據：`red-desktop-fatal.log`、`red-ios-fatal-capture.log`、`red-ios-jb-fatal-capture.log`、`red-ios-jb-duplicate-length.log`、`red-android-regressions.log`；對應的局部 green logs 亦保留。

## 逐檔 ledger（76/76）

| 路徑 | 完整審查結論 |
|---|---|
| `.gitattributes` | 完整讀取；wrapper shell/Gradle 固定 LF、BAT 固定 CRLF，設定與實際用途一致。 |
| `.github/workflows/checks.yml` | 完整讀取；最小 `contents: read`、矩陣測試、JDK/SDK 與 timeout 已核對；actionlint 通過。 |
| `.github/workflows/nightly.yml` | 完整讀取；checks→signing→build→publish、secret 清理、簽章/hash、job 權限與固定 action SHA 已追蹤；未執行發布。 |
| `.gitignore` | 完整讀取；狀態、lock、logs、captures、venv、簽署金鑰與 build 產物均排除。 |
| `AUDIT.md` | 完整讀取全部歷史輪次；歷史效能／真機資料沒有冒充本輪重測結果。 |
| `README.md` | 完整讀取安裝、四版本、CI、離線驗證與風險；命令與目前入口交叉核對。 |
| `RESEARCH.md` | 完整讀取舊實機畫面、平台規則與未確認事項；標記為歷史研究而非本輪現況。 |
| `android/app/build.gradle.kts` | 完整讀取 SDK、Java、簽署條件、debug test 與固定相依；實際 build/lint 覆蓋。 |
| `android/app/src/main/AndroidManifest.xml` | 完整讀取 queries、Activity、AccessibilityService permission/exported 與備份規則；APK badging 交叉核對。 |
| `android/app/src/main/java/tw/avianjay/autocoin/AppLog.kt` | 完整讀取 bounded log、時間戳與 UI callback；未見新缺陷。 |
| `android/app/src/main/java/tw/avianjay/autocoin/Bot.kt` | 完整讀取導航、領取、轉盤、面板、journal、跨日與主迴圈；修正 popup 高度軸並新增 pure 回歸。 |
| `android/app/src/main/java/tw/avianjay/autocoin/CoinService.kt` | 完整讀取 service/worker/overlay/snapshot/action 生命週期；修正主執行緒 action TOCTOU、世代重驗與 ambiguous gesture fatal。 |
| `android/app/src/main/java/tw/avianjay/autocoin/Jitter.kt` | 完整讀取等待、整數、點擊與只上抖動；邊界與呼叫端一致。 |
| `android/app/src/main/java/tw/avianjay/autocoin/MainActivity.kt` | 完整讀取繁中控制、設定、統計與 service weak reference；未見新可重現缺陷。 |
| `android/app/src/main/java/tw/avianjay/autocoin/Screens.kt` | 完整讀取 screen/task/card/panel/withdraw parser 與數字邊界；與 Bot 消費路徑交叉核對。 |
| `android/app/src/main/java/tw/avianjay/autocoin/Store.kt` | 完整讀取 JSON validation、commit、rollback、跨日與 unresolved withdrawal；既有回歸覆蓋。 |
| `android/app/src/main/java/tw/avianjay/autocoin/Ui.kt` | 完整讀取節點 copy/recycle、可信文字、索引與 dump；未見新缺陷。 |
| `android/app/src/main/res/values/strings.xml` | 完整讀取 3 個繁中字串及格式參數；XML 解析通過。 |
| `android/app/src/main/res/xml/accessibility_service.xml` | 完整讀取 events、flags、gesture/content/window 權限；XML 解析通過。 |
| `android/app/src/main/res/xml/backup_rules.xml` | 完整讀取各 domain 排除；敏感狀態不做 cloud backup。 |
| `android/app/src/main/res/xml/data_extraction_rules.xml` | 完整讀取 cloud/device-transfer 排除；與 manifest 參照一致。 |
| `android/app/src/test/java/tw/avianjay/autocoin/SafetyRegressionTest.kt` | 完整讀取所有 37 個 JVM 案例與 fake context/phone；新增 worker 世代及垂直尺度 red→green。 |
| `android/build.gradle.kts` | 完整讀取 AGP 9.1.1 plugin 宣告；版本固定。 |
| `android/gradle.properties` | 完整讀取 JVM/encoding/cache/Kotlin 設定；保留既有 Gradle 10 棄用警告。 |
| `android/gradle/wrapper/gradle-wrapper.jar` | 二進位按類型完整核對；47,505 bytes、34 entries、CRC/entry hash/JAR hash/manifest/實際執行均通過，未冒稱上游原始碼逐行審查。 |
| `android/gradle/wrapper/gradle-wrapper.properties` | 完整讀取 HTTPS distribution、SHA-256、timeout 與 URL validation；Gradle 9.7.1 實際執行。 |
| `android/gradlew` | 完整讀到 EOF；Git Bash 語法與 LF 核對，實際 wrapper/build 成功。 |
| `android/gradlew.bat` | 完整讀到 EOF；CRLF 與 Windows launcher 邏輯核對，實際執行全部 Android gates。 |
| `android/settings.gradle.kts` | 完整讀取 plugin/dependency repositories、`FAIL_ON_PROJECT_REPOS` 與 `:app` include。 |
| `autocoin/__init__.py` | 完整讀取；只有套件說明，無副作用。 |
| `autocoin/__main__.py` | 完整讀取 CLI validation、延遲 driver import、state lock 與 exit 邊界；配合 `Bot.run` 修正 fatal 非零退出。 |
| `autocoin/bot.py` | 完整讀取目前 908 行 deadline、前景、房間、面板、提領與換日；修正 fatal 被吞，保留到期正常結束。 |
| `autocoin/config.py` | 完整讀取 dataclass/env/default limits；CLI 額外驗證已覆蓋。 |
| `autocoin/device.py` | 完整讀取 ADB 選擇、foreground/lock guard、不重播 input 與 app control；離線 fake 回歸通過。 |
| `autocoin/screens.py` | 完整讀取 UI screen/parser/countdown/amount；畸形與非有限輸入回歸通過。 |
| `autocoin/state.py` | 完整讀取 schema、atomic replace/fsync、台北換日、journal 與跨程序 lock；失敗封鎖不清零。 |
| `autocoin/ui.py` | 完整讀取安全 XML parser、immutable snapshot 與索引；效能 benchmark 本輪未重跑。 |
| `docs/reviews/round3-file-review.md` | 完整讀取前輪 74 檔方法、證據與限制；本輪另以目前 76 路徑重建 ledger。 |
| `docs/reviews/takeover-full-review.md` | 本文件；完成後全文回讀，並與 tracked + nonignored untracked 清單做精確零漏項比對。 |
| `ios-app/README.md` | 完整讀取；明確記錄缺 Xcode project、App 入口與 TouchSynthesizer，不能產出 IPA。 |
| `ios-app/Sources/Autocoin/Bot.swift` | 完整讀取 739 行；追蹤停止、風控、面板、journal、跨日。送出前 `save` 後會 `checkHealth`，候選風險排除；僅靜態。 |
| `ios-app/Sources/Autocoin/Jitter.swift` | 完整讀取所有抖動函式；未見新可達缺陷。 |
| `ios-app/Sources/Autocoin/Log.swift` | 完整讀取 serial queue、bounded cache、rotation 與 handle 釋放；僅靜態。 |
| `ios-app/Sources/Autocoin/Phone.swift` | 完整讀取 main-thread bridge、timeout/cancel 與 callback；外部 TouchSynthesizer 缺失，僅語法驗證。 |
| `ios-app/Sources/Autocoin/Screens.swift` | 完整讀取 OCR screen/amount/options parser；與 Python 同型保護交叉核對，僅靜態。 |
| `ios-app/Sources/Autocoin/State.swift` | 完整讀取 Codable defaults、validation、health latch、atomic write 與跨日 journal；僅靜態。 |
| `ios-app/Sources/Autocoin/VisionOCR.swift` | 完整讀取 Vision/crop/pixel allocation/provider release；缺 Apple SDK/runtime。 |
| `ios-jb/README.md` | 完整讀取部署、協定、控制頁、dry-run、取消與風險；未把外部 tweak 視為本庫已驗證。 |
| `ios-jb/bot.py` | 完整讀取目前 962 行 ZXTouch 控制流程；修正 fatal 傳播與診斷清理遮蔽，保留停止檔/到期正常結束。 |
| `ios-jb/com.autocoin.trigger.plist` | 完整讀取 mobile user、路徑、KeepAlive 與 logs；plist/XML 解析通過，未在 launchd 實跑。 |
| `ios-jb/jitter.py` | 完整讀取 wobble/up/int；只上抖動不縮短觀看門檻。 |
| `ios-jb/probe.py` | 完整讀取唯讀 OCR probe、可選手動操作與 shared state lock；未連 tweak。 |
| `ios-jb/screens.py` | 完整讀取 OCR 畫面、金額、面額與幾何 parser；畸形輸入回歸通過。 |
| `ios-jb/selftest.py` | 完整讀取 fake ZXTouch 與 36 個 loopback checks；全過但不等同真機。 |
| `ios-jb/serve.py` | 完整讀取 CSRF/Host/Origin/body limits、bounded threads/logs 與 process race；修正 duplicate Content-Length request ambiguity。 |
| `ios-jb/start.sh` | 完整讀取；cwd/nohup 啟動正確，Git Bash `bash -n` 通過。 |
| `ios-jb/stop.sh` | 完整讀取；只走 authenticated loopback stop，Git Bash `bash -n` 通過。 |
| `ios-jb/tests/test_safety.py` | 完整讀取 58 個測試；新增 capture lock、fatal boundary、raw duplicate length red→green。 |
| `ios-jb/zxtouch.py` | 完整讀取 framing/buffer/deadline/UTF-8/OCR geometry/touch release；socketpair 回歸全過。 |
| `ios/bot.py` | 完整讀取目前 813 行 OCR/TouchSynthesis 流程；修正 capture cleanup、fatal 傳播與 CLI exit 1。 |
| `ios/claim_test.py` | 完整讀取倒數與單次領取 helper；全程 shared state lock，未真機執行。 |
| `ios/explore.py` | 完整讀取手動 tap/swipe/shot helper 與 lock；未真機執行。 |
| `ios/ocr.py` | 完整讀取 WinRT OCR、bitmap 關閉與座標縮放；Windows OCR 真實元件未呼叫。 |
| `ios/requirements.txt` | 完整讀取 11 行 WinRT/Pillow/numpy 相依；pip check/audit 以當次環境驗證。 |
| `ios/scan_rooms.py` | 完整讀取房間掃描 helper、swipe 與 lock；未真機執行。 |
| `ios/screens.py` | 完整讀取 OCR screen/amount parser；幾何、聊天排除與畸形數值回歸通過。 |
| `ios/tests/test_safety.py` | 完整讀取 50 個回歸；新增清理遮蔽、fatal 傳播與 main exit 1 red→green。 |
| `ios/tsclient.py` | 完整讀取 transaction lock、總 deadline、frame/payload/ID validation 與 CLI lock；fake socket 全過。 |
| `pyproject.toml` | 完整讀取 pytest testpaths 與 Ruff 規則；iOS suites 由獨立命令涵蓋。 |
| `requirements-dev.txt` | 完整讀取 pytest/Ruff/Pillow/numpy/pip-audit 範圍；pip check/audit 通過。 |
| `requirements.txt` | 完整讀取 uiautomator2/lxml 下限；實際解析環境通過 audit，未盲目改版。 |
| `tests/test_bot.py` | 完整讀取 fake device/clock 與 transaction tests；新增 StopBot/UnsafeAction/StateError 必須到 CLI 邊界的 3 個案例。 |
| `tests/test_cli.py` | 完整讀取 subprocess help/status 與參數 validation；無第三方套件路徑全過。 |
| `tests/test_state.py` | 完整讀取 schema、atomic replace、跨程序 lock、換日與 rollback；全過。 |
| `tests/test_ui_device.py` | 完整讀取 XML、索引、bounds、ADB non-replay 與 foreground/lock guard；全過。 |
| `tools/benchmark_ui.py` | 完整讀取 baseline、1,800 nodes、90 lookups、7 組中位數方法；因本輪無效能修改且其他專案仍在建置，未量測、不宣稱新倍數。 |

## 保留限制

- 未做真機 E2E；新版 Shopee UI、Android 無障礙 callback、外部 TouchSynthesis/ZXTouch、真實 OCR、網路延遲及帳戶風控仍需隔離真機確認。這些離線 gates 不能證明登入或領取成功。
- `ios-app/` 仍缺 Xcode project、App 入口與 `TouchSynthesizer`；tree-sitter 只證明語法樹無 error，不是 Apple SDK 型別、連結、runtime 或 UI 驗證。
- TouchSynthesis/ZXTouch 協定本身沒有認證／加密；本庫 client 無法單方補足 server 身分。loopback HTTP 已加固，但仍只適合本機使用。
- 未設定或讀取 release signing secrets，debug APK 只用 Android debug certificate；沒有測試 Nightly 實際發布與 release asset 原子性。
- Gradle wrapper JAR 依二進位供應鏈方式核對，沒有反編譯後聲稱等同完整上游原始碼審查；Gradle 10 棄用警告仍存在。
- 本輪沒有效能路徑修改；依協調要求不與 Rust/Flutter builds 同時做 benchmark，保留歷史數據但不新增效能主張。
