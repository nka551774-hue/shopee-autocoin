# 蝦皮直播自動領蝦幣

在蝦皮直播間自動領取：

- **直播間蝦幣**：倒數到了就按「領取」。
- **幸運轉盤**：限粉絲的會自動關注。
- **觀看直播任務**：定時開任務面板，把累積的蝦幣領進「待提領」。
- **提領**（可關閉）：把「待提領」轉進「我的蝦幣」。

有四個版本，邏輯相同：

| | 電腦版 | 手機版 | iOS 版 | iOS 越獄版 |
|---|---|---|---|---|
| 位置 | `autocoin/`（Python） | `android/`（Kotlin App「畫面輔助」） | `ios/`（Python） | `ios-jb/`（Python，跑在手機上） |
| 怎麼控制手機 | ADB + uiautomator2 | 手機上的無障礙服務 | iPhone 上的 [TouchSynthesis](https://github.com/AnotherAvianJay/TouchSynthesis) + Windows OCR | iPhone 上的 ZXTouch（越獄，HID 觸控 + Vision OCR） |
| 需要電腦開著 | 要 | 不用 | 要 | 不用 |

研究過程與畫面元件的細節在 [RESEARCH.md](RESEARCH.md)。

2026-10-25 的漏洞修正、效能量測、驗證方式及剩餘限制見 [AUDIT.md](AUDIT.md)。
第三輪的逐檔覆蓋與重跑環境見 [完整檔案審查紀錄](docs/reviews/round3-file-review.md)。
`ios-app/` 是尚未有完整建置專案的 Swift 原始碼，狀態見 [ios-app/README.md](ios-app/README.md)。

## 它會做什麼

1. 打開直播間右上角的「看更多」列表，挑一間直播間進去，優先順序如下：
   - 轉盤次數還有剩：先挑標「抽獎」的。
   - 否則挑卡片上「最多X」最高的。
2. 待在那間：
   - 倒數到了就領，一直領到這場沒有直播間蝦幣為止。
   - 中途**不會離開**，因為離開會讓倒數重來。
3. 有轉盤就轉。每間一次，一天 10 次。
4. 每 30 分鐘開一次任務獎勵面板（間隔有 ±25% 抖動），領觀看任務的蝦幣。23:35~23:50 之間會再開一次，因為這筆要當天領。
5. 提領（可關閉）的條件：
   - 待提領 ≥ 5 時，選不超過餘額的最大面額。
   - 收尾時只要剩 ≥ 1 也會提領。
   - **每天最多 3 次**。如果你手動提領過，蝦皮回報上限時程式也會停止提領。
6. 直播間蝦幣用完當天的 100 次、轉盤也用完時，改成每間待 10 分鐘累積觀看任務（每天 90 分鐘）。全部做完就休息到隔天。
7. 蝦皮回報「不符合領取此獎勵資格」「偵測到風險活動」「已達裝置數限制」時，**立刻停止**。

## 電腦版

```powershell
cd D:\proj\shopee-autocoin
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

.venv\Scripts\python -m autocoin run            # 開始（Ctrl+C 停止）
.venv\Scripts\python -m autocoin run --hours 3  # 跑 3 小時
.venv\Scripts\python -m autocoin run --no-withdraw  # 不自動提領
.venv\Scripts\python -m autocoin status         # 今天的統計
.venv\Scripts\python -m autocoin scan           # 列出「看更多」裡的直播間
.venv\Scripts\python -m autocoin panel          # 開一次任務面板（領觀看任務、視情況提領）
```

- 以 `--serial` 或 `AUTOCOIN_SERIAL` 指定裝置；未指定時使用 `ANDROID_SERIAL` 或唯一一台 ADB 裝置，多台裝置會要求明確選擇。ADB server 預設連 5037，可用 `--adb-port` 覆蓋，不受 `ANDROID_ADB_SERVER_PORT` 影響。
- 紀錄檔在 `logs/autocoin.log`，當天統計在 `state.json`。
- 遇到沒看過的畫面，截圖和 UI 樹會存在 `logs/captures/`。
- 執行中手機會被操作，**不要同時用手機**。
- 建議開啟「充電時保持螢幕常亮」，避免鎖屏中斷。
- `--hours` 必須是有限正數，時間到後停止操作，亦不會再觸發收尾提領；需要手動收尾可在確認畫面後使用 `panel`。
- 同一份統計檔只允許一個操作程序。統計檔損壞會停止，不會把每日次數歸零。未確認的提領會記入 `withdrawal_pending`，重啟或跨日也不會自動再送；請依 [AUDIT.md](AUDIT.md) 核對後復原。

## 手機版（畫面輔助）

```powershell
cd D:\proj\shopee-autocoin\android
.\gradlew.bat assembleDebug
adb -P 5037 install -r app\build\outputs\apk\debug\app-debug.apk
```

1. 開啟無障礙服務：設定 → 其他設定 → 無障礙 → 畫面輔助 → 開啟。也可以在 App 裡按「開啟無障礙設定」。
2. 開始的方式有兩種：
   - 打開 App 按「開始」。
   - 點畫面最下面的小黑條「蝦幣 ■ 已停止」。
3. App 會自己打開蝦皮開始領。要用手機時，點小黑條或在 App 裡按「停止」。
4. 「自動提領」開關在 App 主畫面，關掉就只領不提領。
5. 執行中，小黑條會讓螢幕保持亮著。如果手機被鎖住，程式會等你解鎖再繼續。

## iOS 版

iPhone 上裝 TouchSynthesis（AnotherAvianJay 的 fork，CI 產的 IPA 用 SideStore 裝），電腦跟手機在同一個 Wi-Fi。需要 Windows 的「中文(台灣)」OCR 元件。

1. 手機：「自動鎖定」設成「永不」，打開 TouchSynthesis 按 Start UI Automation → Start Server，再切到蝦皮的「直播」分頁。
2. 電腦：

```powershell
.venv\Scripts\pip install -r ios\requirements.txt
$env:TS_HOST = "192.168.31.196"                 # 手機的 IP
.venv\Scripts\python ios\bot.py run             # 開始（Ctrl+C 停止）
.venv\Scripts\python ios\bot.py run --hours 3 --no-withdraw
.venv\Scripts\python ios\bot.py status          # 今天的統計
.venv\Scripts\python ios\bot.py panel           # 開一次任務面板
.venv\Scripts\python ios\bot.py look            # 印出目前畫面的判讀結果（除錯用）
```

跟 Android 版的差別：

- iOS 讀不到元件樹，畫面全靠截圖 OCR。「領取」按鈕看的是顏色（直播間是橘色，面板是黃色）。
- 沒有「看更多」列表，改成往上滑換台，找有「直播間蝦幣」的直播間。滑了 15 間都沒有的話，就在當下那間累積觀看任務。
- 觀看任務的倒數停住 90 秒，就當作這間的 10 分鐘已經算滿。
- 轉盤、紅包雨還沒做。
- 開不了 App。畫面認不得時會等待你把手機切回蝦皮直播間；返回操作只用於已辨識的面板。
- 等待與點擊沒有抖動。
- 統計存在 `state-ios.json`（iOS 跟 Android 是不同的蝦皮帳號），紀錄檔在 `logs/ios.log`，沒看過的畫面存在 `logs/captures-ios/`。
- `claim_test.py`、`explore.py`、`scan_rooms.py` 與 `tsclient.py` 的 CLI 也會持有同一份裝置操作鎖；使用前先停止 bot，鎖檔不需要刪除。

## iOS 越獄版（ios-jb/）

越獄（Dopamine 16.1.1 測過）之後，觸控與 OCR 都交給手機上的 ZXTouch，**完全不用電腦**：

```sh
cd /var/mobile/shopee-autocoin/ios-jb
python3 probe.py            # 先驗：只讀畫面，不點東西
python3 bot.py run          # 開始（Ctrl-C 停止）
sh start.sh                 # 一鍵開始的觸發器（Safari 加入主畫面後點一下）
```

裝法、一鍵開始的設定、以及「這一版 tweak 沒有取色／shell」等已查證的細節，都在
[ios-jb/README.md](ios-jb/README.md)。風險提醒：越獄本身就是可被偵測的特徵。

## Nightly 建置（GitHub Actions）

`.github/workflows/nightly.yml` 會在 push 到 `main`、每天台灣時間 02:00、或手動觸發時建 signed release APK，然後**更新同一個 `nightly` release**（固定檔名 `autocoin-nightly.apk`，每次覆蓋，不會愈積愈多）。

發布前先執行 Windows／Linux Python 回歸測試、依賴漏洞檢查及 Android 測試、Lint、debug 建置。
簽署與發布只接受 `main`；fork 未設定簽署 secrets 時會略過發布，仍保留測試結果。
簽署 APK 經簽章與 SHA-256 檢查後才發布；發布工作才有 repository 寫入權限，簽署金鑰在建置後刪除。

需要在 repo 的 Settings → Secrets and variables → Actions 設好這三個：

- `KEYSTORE_BASE64`：`base64 -w0 release.jks` 的輸出
- `KEYSTORE_ALIAS`
- `KEYSTORE_PASSWORD`（同時當 store password 和 key password）

本機沒設這三個環境變數時，`build.gradle.kts` 不會建立 signing config，`assembleDebug` 照常可用。

## 離線驗證

Python 3.11，可在沒有連接手機、沒有登入帳戶的情況執行：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip setuptools
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m unittest discover -s ios/tests -v
.venv\Scripts\python -m unittest discover -s ios-jb/tests -v
.venv\Scripts\python ios-jb/selftest.py
.venv\Scripts\python -m ruff check autocoin ios ios-jb tests tools
.venv\Scripts\python -m pip_audit --progress-spinner off
.venv\Scripts\python tools/benchmark_ui.py
```

Android 需 JDK 21 與 Android 36 SDK：`cd android` 後執行
`.\gradlew.bat testDebugUnitTest lintDebug assembleDebug`。Windows 建議使用純英文路徑；
某些 Gradle 測試 worker 在中文路徑無法載入已編譯的 class。

## 降低機器指紋

風控不只看你裝了什麼，也看**行為的規律性**。所以等待時間與點擊座標都加了隨機抖動：

- 所有等待走 `Jitter.ms()`（Kotlin）/ `doze()`（Python），預設 ±18%。
- 點擊不再落在節點正中央，改成中心 ±9 px 的隨機點；滑動端點也一起位移。
- 每個直播間的上限只往上抽（`Jitter.up()`）：觀看任務的 10 分鐘門檻往下抖會做不完，往上抖只是多待一下。
- 面板間隔 ±25%，收尾時間從固定的 23:40 改成 23:35~23:50 之間每天固定一個值。
- 倒數醒來的提前量從固定 2 秒改成 1~6 秒隨機。

抖動量可調：Python 在 `autocoin/config.py` 的 `jitter` / `tap_jitter_px`，Kotlin 在 `Jitter.kt` 的預設參數。

另外無障礙服務的 `app_name` / `service_description` 刻意寫成中性的「畫面輔助」，因為 `getInstalledAccessibilityServiceList()` 會把這兩個字串連同 package 一起讀走。

## 風險

蝦皮服務條款 6.2(k) 禁止用機器人等軟體使用平台。官方說明也寫了，偵測到風險時會拒絕領取或讓領取按鈕消失。使用前請自行評估。
