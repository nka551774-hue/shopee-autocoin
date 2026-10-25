# 第三輪逐檔審查紀錄

日期：2026-10-25（Asia/Taipei）。專案：shopee-autocoin；分支：`main`；基準：`c5edbec3e0a6c37e25b375e24a84fcbf71fe5613`。

## 覆蓋與方法

本輪以基準的 `git ls-files` 74 個檔案建立清單，完整閱讀 71 個 first-party 文字檔（來源、測試、設定、CI、部署腳本與文件），逐項追蹤裝置輸入、畫面解析、狀態鎖、配額、午夜、取消、網路與控制器生命週期。另 3 個 Gradle 生成／第三方檔案分列為供應鏈驗證；沒有將檔名掃描、搜尋命中或二進位雜湊說成逐行原始碼審查。本文件是本輪新增，並非基準 74 檔之一。

Python／Android 可執行缺陷先加測試並觀察原碼失敗，再修復；新增正常路徑測試則驗證鎖涵蓋整段操作並於例外後釋放。Swift 缺完整建置專案與工具鏈，只有靜態審查／tree-sitter 語法解析，沒有 Swift runtime 的 red/green 證據。

## 本輪缺陷與證據

| 缺陷 | 修正與證據 |
|---|---|
| iOS 研究工具與 ZXTouch probe 可在 bot 持鎖時連線並輸入 | 5 個 CLI 現持有同一 `state-ios.json` 鎖，取鎖前不連線。真實鎖衝突驗證無連線、原始狀態 bytes 不變；另驗證操作內持鎖與例外釋放。 |
| 控制頁先切前景再驗證裝置擁有權 | `bring_shopee_front` 取共享鎖後才連線，並再看取消旗標。持鎖期間控制器不得連 ZXTouch。 |
| iOS 聊天區／嵌入文字被判為面板或提領頁 | 僅接受聊天區上方的完整標籤。使用正式 parser 驗證 4 個偽標籤輸入。 |
| iOS 金額接受負數、三位小數前綴、連續小數或 float infinity，面額接受負號或任意文字前綴 | 兩種 Python iOS parser 改為數字邊界、有限數值與完整正面額匹配；保留有效「待提領蝦幣數: 3·00」與原 selftest。Swift 同型來源同步修正，但僅靜態驗證。 |
| Android 領取後畫面不明／逾時、觀看任務領取未確認時返回主迴圈 | 改為拋 `StopBot`，未加成功計數，不允許下一輪重複點擊。2 個 JVM 回歸原碼失敗。 |
| ios-jb 未知 App 的「稍後再說」會被點擊 | 先確認畫面已辨識；未知畫面測試沒有任何 tap。 |
| ios-jb 先前已存在的獎勵通知可確認新的觀看領取 | 記住點擊前通知，只接受新通知或正餘額差；重新辨識領取後面板；結果不明即停止且不加成功計數。 |
| Swift 使用跨午夜前的 Day、缺送出前畫面／餘額／額度再驗、確認先於拒絕、未知頁成功提示、收尾跨日多睡一天 | 同步修正來源。此部分未經 Apple SDK 型別檢查、連結或執行，不把 Python 同型測試當 Swift 測試。 |

原碼失敗輸出（本機未追蹤）：`.git/shopee-round3-ios-red.log`（46 tests，12 failures／1 error，其中舊 tsclient CLI 尚無可呼叫 main）、`.git/shopee-round3-jb-red.log`（52 tests，4 failures）、`.git/shopee-round3-jb-red2.log`（55 tests，4 failures）、`.git/shopee-round3-android-red-corrected.log`（35 tests，2 failures）。Android 最初新增的觀看測試誤用 iOS 面板標籤，第一次修正版測試因此仍失敗；已更正為 Android 正式 parser 使用的「待提領蝦幣數」，並將基準 Bot.kt 僅放進 ASCII 建置副本，重新確認兩項確實 red，再同步工作樹修正版跑 green。初次錯 fixture 不當作有效缺陷證據。

## 最終驗證結果

| 命令／範圍 | 本輪實際結果 |
|---|---|
| pytest | 77 passed |
| iOS unittest | 47 passed |
| ios-jb unittest | 55 passed |
| Android JVM | 35 tests，0 failures／errors／skipped |
| ios-jb selftest | 36 項 loopback 檢查通過 |
| Android lintDebug／assembleDebug | Lint 0 issue；BUILD SUCCESSFUL |
| Ruff、pip check、pip-audit | 全過；當次依賴資料庫未發現已知漏洞 |
| Swift tree-sitter | 7 個來源檔語法無 error；不是型別或 runtime 測試 |
| 逐檔清單與 Git diff | 基準 74/74 路徑完全吻合；`git diff --check` 通過 |

合計 **214 項可執行回歸測試**，另 **36 項 loopback 檢查**。22 個 Git 追蹤 Android 檔案與最終 ASCII 副本 SHA-256 全數一致。debug APK SHA-256：`9EE47DC91BA3B9EF5810D3F57B78E8D800E0165C66720B2922B563DC25E89D43`。原有 Gradle 10 相容性棄用警告仍在。

本機成功紀錄：`.git/shopee-round3-pytest.log`、`.git/shopee-round3-ios-final.log`、`.git/shopee-round3-jb-final.log`、`.git/shopee-round3-selftest.log`、`.git/shopee-round3-pip-audit.log`、`.git/shopee-round3-swift-syntax.log`、`.git/shopee-round3-android-final2.log`。Android XML 在下列 `$build/app/build/test-results/testDebugUnitTest/` 與 `$build/app/build/reports/lint-results-debug.xml`。本機紀錄未提交；命令可重新執行。

主代理另行複審本輪 diff，指出 Swift 初次打開提領仍可能忽略新 snapshot 而使用舊面板座標；現改為無面板參數的 `withdraw()`，每次點擊前用最新 snapshot 確認面板並重新解析按鈕，重試也使用等待後的新畫面。7 檔語法檢查已在此修改後重跑通過；沒有冒稱 Swift runtime 回歸。

## 重跑方式與環境

工作目錄為 `C:\Users\User\Desktop\fork 的地方\shopee-autocoin`。PowerShell、Python 3.11.9，命令皆未連真實手機、領取或提領。

```powershell
$env:PYTHONUTF8 = '1'
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m unittest discover -s ios/tests -q
.venv/Scripts/python.exe -m unittest discover -s ios-jb/tests -q
.venv/Scripts/python.exe ios-jb/selftest.py
.venv/Scripts/python.exe -m ruff check autocoin ios ios-jb tests tools
.venv/Scripts/python.exe -m pip check
.venv/Scripts/python.exe -m pip_audit --progress-spinner off
git diff --check
```

Android 避開中文 classpath 問題，使用既有 ASCII 暫存副本。只同步 Git 追蹤的 Android 檔案，不複製原工作樹的 build／.gradle。

```powershell
$build = 'C:\Users\User\AppData\Local\Temp\autocoin-review-android-4b394354'
git ls-files android | ForEach-Object {
    $dst = Join-Path $build $_.Substring(8)
    New-Item -ItemType Directory -Path (Split-Path $dst) -Force | Out-Null
    Copy-Item -LiteralPath $_ -Destination $dst -Force
}
$env:JAVA_HOME = 'C:\Program Files\Java\jdk-21.0.10'
$env:ANDROID_HOME = 'C:\Users\User\AppData\Local\Temp\autocoin-audit-android-sdk'
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
& "$build\gradlew.bat" --no-daemon --console=plain -p $build testDebugUnitTest lintDebug assembleDebug
git ls-files android | ForEach-Object {
    if ((Get-FileHash -LiteralPath $_).Hash -ne
        (Get-FileHash -LiteralPath (Join-Path $build $_.Substring(8))).Hash) {
        throw "建置副本不一致：$_"
    }
}
```

Swift 語法檢查另使用本機 `tree-sitter==0.26.0`、`tree-sitter-swift==0.7.4`；它們不是 App 編譯器。若重建此檢查環境，先在驗證 venv 安裝這兩個版本，再執行：

```powershell
$script = @'
from pathlib import Path
from tree_sitter import Language, Parser
import tree_sitter_swift
parser = Parser(Language(tree_sitter_swift.language()))
files = sorted(Path("ios-app/Sources/Autocoin").glob("*.swift"))
for path in files:
    assert not parser.parse(path.read_bytes()).root_node.has_error, path
print(f"{len(files)} Swift files parsed; syntax only")
'@
.venv/Scripts/python.exe -c $script
```

## 逐檔結果

| 檔案 | 完整閱讀／核對結果 |
|---|---|
| `.gitattributes` | 已逐行讀取；Shell/Gradle wrapper 固定 LF、BAT 固定 CRLF，未見新缺陷。 |
| `.github/workflows/checks.yml` | 已逐行追蹤 Python matrix、JVM/Lint/build、權限及 timeout；main 交由 Nightly reusable checks，未見本輪新缺陷。 |
| `.github/workflows/nightly.yml` | 已逐行追蹤 checks→signing→build→publish、secret 清理、APK 簽章/hash、tag 更新；保留個別 release asset 非原子替換限制。 |
| `.gitignore` | 已逐行讀取；狀態、鎖、暫存、環境檔、簽署金鑰及 build 產物均排除，未見新缺陷。 |
| `android/app/build.gradle.kts` | 已逐行核對 SDK/JVM 版本、簽署欄位、debug unit test 設定與兩個測試相依；本輪以實際 build 驗證。 |
| `android/app/src/main/AndroidManifest.xml` | 已逐行核對 Shopee/home queries、服務 permission/exported、Activity、備份排除與資源引用。 |
| `android/app/src/main/java/tw/avianjay/autocoin/AppLog.kt` | 已逐行讀取容量限制、時間戳與UI通知；未見新缺陷。 |
| `android/app/src/main/java/tw/avianjay/autocoin/Bot.kt` | 完整逐行追蹤所有導航、額度、主迴圈/跨日及提領流程；本輪驗證領取不明仍返回迴圈的重點擊路徑。 |
| `android/app/src/main/java/tw/avianjay/autocoin/CoinService.kt` | 已完整逐行追蹤 start/stop worker所有權、手勢取消、前景/鎖定guard、snapshot資源回收與overlay生命週期；真機服務驗證仍未覆蓋。 |
| `android/app/src/main/java/tw/avianjay/autocoin/Jitter.kt` | 已逐行核對整數/秒數/上抖動；零pct無正式呼叫，未當作可達缺陷。 |
| `android/app/src/main/java/tw/avianjay/autocoin/MainActivity.kt` | 逐行核對繁中控制項、service weak ref、resume/pause handler及日/狀態/log快取；未見新缺陷。 |
| `android/app/src/main/java/tw/avianjay/autocoin/Screens.kt` | 已逐行核對全頁與panel/withdraw辨識、金額/每日計數解析；與Bot路徑交叉檢查。 |
| `android/app/src/main/java/tw/avianjay/autocoin/Store.kt` | 完整追蹤JSON型別/額度、rollback、SharedPreferences同步commit、跨日journal；現有測試對應。 |
| `android/app/src/main/java/tw/avianjay/autocoin/Ui.kt` | 已逐行核對 package 篩選、tree遍歷、copy/recycle與文字查找；未見新可達缺陷。 |
| `android/app/src/main/res/values/strings.xml` | 已讀取三個字串及格式參數；與 service UI 呼叫相符。 |
| `android/app/src/main/res/xml/accessibility_service.xml` | 已逐行核對 event types、view IDs、interactive windows、gesture/content 權限。 |
| `android/app/src/main/res/xml/backup_rules.xml` | 已讀取各 backup domain 排除；帳號狀態不移植至其他裝置。 |
| `android/app/src/main/res/xml/data_extraction_rules.xml` | 已讀取 cloud/device-transfer 所有排除域；與 manifest 關聯一致。 |
| `android/app/src/test/java/tw/avianjay/autocoin/SafetyRegressionTest.kt` | 完整閱讀33個既有JVM案例、fake phone/preferences及反射；新增領取不明停止回歸。 |
| `android/build.gradle.kts` | 已讀取 AGP 9.1.1 插件宣告；未另套重複 Kotlin 插件。 |
| `android/gradle.properties` | 已讀取 heap/encoding/cache/Kotlin 設定；保留既有 Gradle 10 警告。 |
| `android/gradle/wrapper/gradle-wrapper.jar` | 第三方二進位47505bytes；核對SHA256/副本一致性並實際執行，未反編譯或宣稱原始碼覆蓋。 |
| `android/gradle/wrapper/gradle-wrapper.properties` | 已逐行核對 HTTPS distribution、SHA-256、timeout/validation；wrapper 執行由 Android 驗證覆蓋。 |
| `android/gradlew` | Gradle生成的第三方wrapper shell，分類為供應鏈檔；未作first-party逐行深審，版本/行尾/內容hash與建置副本一致性檢查。 |
| `android/gradlew.bat` | Gradle生成的第三方wrapper BAT，分類為供應鏈檔；由實際JVM/lint/build執行驗證與hash核對，未冒稱first-party深審。 |
| `android/settings.gradle.kts` | 已讀取 plugin 與 dependency repository、禁止 project repo 及 :app 包含；未見新缺陷。 |
| `AUDIT.md` | 完整閱讀139行所有歷史輪次、命令及限制；本輪新增第三輪章節並區分Swift靜態修正與可執行red-green。 |
| `autocoin/__init__.py` | 已讀取套件說明，無執行副作用。 |
| `autocoin/__main__.py` | 逐行追蹤參數、status無裝置初始化、其餘CLI鎖與例外路徑；未見新缺陷。 |
| `autocoin/bot.py` | 完整逐行追蹤deadline、裝置guard、房間/轉盤/面板/提領、journal及收尾換日；未見本輪新可重現缺陷。 |
| `autocoin/config.py` | 已逐行核對 dataclass 預設、環境serial、每日3次/100次外部規則；CLI 層驗證另外追蹤。 |
| `autocoin/device.py` | 已逐行讀取連線重試、前景 package guard、action與節點邊界；未見新可重現缺陷，裝置移動畫面的 race 仍需真機。 |
| `autocoin/screens.py` | 已逐行追蹤頁面判斷及數字解析；財務欄位使用完整匹配，後續與 bot 決策交叉檢查。 |
| `autocoin/state.py` | 逐行追蹤 schema 驗證、台北換日、pending journal、獨占鎖、atomic replace/fsync 與失敗傳播；未見新缺陷。 |
| `autocoin/ui.py` | 已逐行讀取 XML 安全解析、節點索引、文字與區域查找；與既有回歸測試對照。 |
| `ios-app/README.md` | 已逐行讀取並核對缺Xcode/入口/TouchSynthesizer之實驗限制；本輪更新修正範圍。 |
| `ios-app/Sources/Autocoin/Bot.swift` | 完整閱讀713行；修復面板畫面重驗、提領送出前再驗、拒絕先於確認、未知頁成功提示、跨日活躍Day與隔日喚醒；僅靜態及語法驗證，無Apple SDK。 |
| `ios-app/Sources/Autocoin/Jitter.swift` | 完整閱讀4個抖動方法與呼叫常數；未見本輪新可達缺陷。 |
| `ios-app/Sources/Autocoin/Log.swift` | 完整讀取serial queue、主執行緒callback、300行cache、檔案rotate及FileHandle釋放；未見本輪新可達缺陷。 |
| `ios-app/Sources/Autocoin/Phone.swift` | 逐行追蹤completion同步、cancel、timeout、主執行緒guard、尺寸與touch callback；缺TouchSynthesizer故只能靜態/語法驗證。 |
| `ios-app/Sources/Autocoin/Screens.swift` | 完整逐行讀取，移植同型修正：聊天標籤、畸形/溢位金額與完整面額匹配；僅靜態及語法驗證。 |
| `ios-app/Sources/Autocoin/State.swift` | 完整追蹤Codable預設/日期驗證/health latch/atomic寫入/跨日journal；無Swift runtime，不宣稱型別或持久化實機驗證。 |
| `ios-app/Sources/Autocoin/VisionOCR.swift` | 完整讀取像素allocation/provider釋放、crop/scale及Vision座標；Apple SDK/runtime未覆蓋。 |
| `ios-jb/bot.py` | 完整閱讀948行各條路徑；修復未知App的評分視窗操作、舊領取通知誤認新成功，以及結果不明未停止；額度/跨日/取消已有回歸。 |
| `ios-jb/com.autocoin.trigger.plist` | 已逐行核對 mobile 使用者、Python/serve 路徑、KeepAlive與log；真實 launchd 未可執行。 |
| `ios-jb/jitter.py` | 已逐行讀取 jitter/up/int/wobble；up 不縮短最低觀看時間。 |
| `ios-jb/probe.py` | 完整逐行閱讀含 unsupported/probe/socket finally；修復全程未持共享裝置鎖，鎖衝突須在連線前拒絕。 |
| `ios-jb/README.md` | 完整閱讀151行部署/複製共用模組/觸發器/取消/dry-run/未確認提領；本輪補helper與前景鎖說明。 |
| `ios-jb/screens.py` | 完整讀取含 read_panel/read_withdraw；修復小數串前綴與溢位金額、負/偽前綴面額。 |
| `ios-jb/selftest.py` | 完整讀取本機fake TCP、畫面資料、36檢查及dry-run；不將其等同真機。 |
| `ios-jb/serve.py` | 完整追蹤HTTP來源驗證/CSRF/大小限制、8 handler上限、程序擁有權、start/stop競態及log drain；修復切前景指令未持裝置鎖。 |
| `ios-jb/start.sh` | 已逐行核對 cwd 與失敗退出；只啟動loopback控制器，不直接執行提領。 |
| `ios-jb/stop.sh` | 已讀取 exec 的驗證停止路徑，後續追蹤 serve.request_stop。 |
| `ios-jb/tests/test_safety.py` | 完整讀取50個既有案例及新增helper鎖、畸形OCR、controller前景鎖、未知畫面與舊通知回歸；red/green log保存在.git。 |
| `ios-jb/zxtouch.py` | 完整讀取framing/deadline/UTF8、OCR geometry、touch release、commands；現有socketpair回歸覆蓋，未連真實tweak。 |
| `ios/bot.py` | 完整逐行追蹤主迴圈、deadline、重連、觸控不重播、每日額度/換日、面板與提領journal；本輪修正其下游畫面/金額辨識。 |
| `ios/claim_test.py` | 已逐行讀取倒數觀察/點擊 helper；本輪修復CLI未持共享state-ios.json鎖，鎖衝突在連線前拒絕，真實鎖回歸通過。 |
| `ios/explore.py` | 已逐行讀取 tap/swipe/shot CLI；本輪修復CLI未持共享state-ios.json鎖，鎖衝突在連線前拒絕，真實鎖回歸通過。 |
| `ios/ocr.py` | 已逐行核對 WinRT OCR、座標縮放與行結構；離線測試不涵蓋真實系統 OCR。 |
| `ios/requirements.txt` | 已逐行讀取 Windows WinRT OCR 依賴；實際 Windows OCR 環境與離線 stub 驗證界線分列。 |
| `ios/scan_rooms.py` | 已逐行讀取房間掃描及 swipe；本輪修復CLI未持共享state-ios.json鎖，鎖衝突在連線前拒絕，真實鎖回歸通過。 |
| `ios/screens.py` | 完整逐行追蹤畫面及金額解析；修復聊天區/嵌入文字誤判、畸形/溢位金額及面額前綴；正式parser回歸red→green。 |
| `ios/tests/test_safety.py` | 完整閱讀舊42項並新增裝置鎖、聊天偽標籤、畸形金額及面額回歸；已觀察red後green。 |
| `ios/tsclient.py` | 已逐行核對封包長度、timeout、畫面與觸控命令；本輪修復CLI未持共享state-ios.json鎖，鎖衝突在連線前拒絕，真實鎖回歸通過。 |
| `pyproject.toml` | 已逐行讀取 pytest testpaths 與 Ruff 選規；ios suites 由獨立 unittest 命令涵蓋。 |
| `README.md` | 完整閱讀173行安裝/版本/驗證/CI/限制，核對入口及執行要求；本輪補helper鎖與審查連結。 |
| `requirements-dev.txt` | 已逐行讀取 test/lint/圖像/audit 相依與版本區間；無盲目升級。 |
| `requirements.txt` | 已讀取 uiautomator2/lxml 下限；本輪 pip check/audit 檢查解析後套件。 |
| `RESEARCH.md` | 完整閱讀104行上游2026-10-08歷史畫面/規則筆記，作為辨識設計資料；未將舊真機觀察或平台規則宣稱今日重新驗證。 |
| `tests/test_bot.py` | 完整閱讀fake clock/UI/device與領取/提領/每日額度/換日回歸，對照正式消費路徑。 |
| `tests/test_cli.py` | 完整讀取無第三方的status/help與非法參數subprocess測試；全程不連手機。 |
| `tests/test_state.py` | 完整讀取schema、atomic replace、真實跨程序鎖、日切與rollback回歸。 |
| `tests/test_ui_device.py` | 完整讀取節點索引/抖動/XML/前景/非重送/解析回歸；實際ADB仍未涵蓋。 |
| `tools/benchmark_ui.py` | 已逐行追蹤 baseline git show、1800節點90查詢、7組中位數及正確性斷言；本輪不量測、不重用舊倍數作新結果。 |

## 未覆蓋與限制

- 外部 Shopee、TouchSynthesis、ZXTouch、Android 無障礙 callback、手機前景與真實 OCR：未作真機端到端測試。沒有安裝 APK。
- Swift：7 個來源檔已完整閱讀，但缺 Xcode project、App 入口、TouchSynthesizer、swiftc 與 Apple SDK。語法通過不代表型別、連結、執行與 UI 可用；這是本輪最主要的未驗證範圍。
- Gradle wrapper：`gradlew`／`gradlew.bat` 為生成的第三方腳本；JAR 未反編譯。JAR SHA-256：`7A9CE74CFF467CA1BF60A4FCD9F05185ACCEDA4D0F382434D393E17864262C5D`。distribution 使用已設定的 HTTPS／SHA-256；本輪未重新審查 Gradle 全部上游原始碼。
- 排他鎖以此 checkout 的狀態檔為範圍，不能協調另一電腦或另一 checkout 的同一手機。OCR 也不能證明圖像文字真的來自平台。
- 平台規則與 RESEARCH 的舊實機結果未在本輪重新確認；依賴稽核只代表當次安裝版本及漏洞資料庫。
- 未執行效能量測、不宣稱新加速倍數；測試／建置沒有同時進行效能 benchmark。既有 Gradle 10 棄用警告保留。
