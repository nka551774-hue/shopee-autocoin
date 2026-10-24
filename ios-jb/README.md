# ios-jb — 在 Dopamine 16.1.1 上「全機自己跑」（不用電腦）

電腦版（`ios/`）要 PC 跑 Python + Windows OCR、手機要跑 TouchSynthesis，而且那條路要
VPN loopback + pairing record + DDI + 密碼。**jailbreak 之後這些全部不需要**：
只有一個 tweak（ZXTouch）＋手機上的 python3，bot 直接在手機上跑。

| | 電腦版（`ios/`） | jailbreak 版（`ios-jb/`） |
|---|---|---|
| 觸控注入 | XCTest / testmanagerd（要 automation session） | ZXTouch 以 root 直接送 HID 事件 |
| 截圖＋OCR | XCTest + Windows.Media.Ocr（PC） | tweak 內部截圖 + Vision.framework（手機上，有座標） |
| 前置 | VPN loopback + pairing record + DDI + 密碼 | 裝一個 tweak，respring |
| 電腦 | 要（跑 bot） | 不用 |
| 一鍵開始 | CLI | 主畫面書籤點一下（見下面） |
| 看顏色 | 有（自己算像素） | **沒有**（這一版 tweak 沒有取色，改用 OCR 讀「領取」兩個字） |

## 已查證的事（別再猜）

我把 [CodeTips/IOS-SimulateTouch](https://github.com/CodeTips/IOS-SimulateTouch) 的原始碼、
以及 repo 裡附的 `com.zjx.pccontrol_0.0.2_iphoneos-arm64e.deb` 拆開看過 dylib，確認：

- **有實作**：10 觸控、11 切前景、12 alert、18 usleep、19/20 播放／停止腳本、22 toast、
  25 裝置資訊、26 觸控指示、27 OCR。
- **沒有實作**（fork 把 `Task.xm` 裡的分支整段註解掉，dylib 裡也查不到字串）：
  13 shell、21 圖像比對、**23 取色、28 找色**。所以畫面判讀不能看顏色。
- **座標全部是像素**：`getScreenSize` 回 `bounds.size * scale`（iPhone 11 Pro = 1125x2436），
  觸控也是除以像素寬高做正規化 → 送 point 會點到左上角 1/3 的位置。
  `screens.py` 裡的框都是 point，送進 tweak 前乘 scale。
- **OCR 回的是像素、左上角原點**（`VKOcrManager` 用 `(1 - topLeft.y)` 換算），
  而且是「一行一筆」不是一個字一筆。
- **協議**：`"<task 2 位數>" + ";;".join(args) + "\r\n"`，task 後面直接接第一個參數
  （`27` + `1;;rect;;…`）。觸控**不會回回應**，其他 task 回 `0;;…` 或 `-1;;錯誤`。
- **這一版的 deb 只有 tweak**：沒有 App、沒有 Shortcuts 擴充、沒有 Activator 監聽器的
  plist、沒有 `/bin/python3`／`zxtouchb`。所以「用 Activator 手勢跑 .bdl 腳本」在這一版
  行不通（`play_script` 的 .py 會去要 `/bin/python3` 跟 `sudo zxtouchb`，兩個都沒有）。

## 裝法（手機上）

1. 裝 deb（Sileo 開 repo 或直接丟檔案）：

   ```sh
   dpkg -i com.zjx.pccontrol_0.0.2_iphoneos-arm64e.deb
   ```

   然後 respring。tweak 會在 `127.0.0.1:6000` 聽 socket。
2. 裝 python3（Sileo 的 `python3`，3.9+ 就好；不用 ZXTouch 內附的 3.7，那個路徑在
   rootless 也不對）。
3. 把專案的 `ios-jb/` 與 `autocoin/` 一起放到手機（例如 `/var/mobile/shopee-autocoin/`）。
   `bot.py` 會使用共用的 `autocoin.config` 與 `autocoin.state`，只複製 `ios-jb/` 會缺少模組。
4. 蝦皮停在「直播」分頁；「設定 > 螢幕顯示與亮度 > 自動鎖定」設成「永不」。

## 先驗一次（重要）

```sh
cd /var/mobile/shopee-autocoin/ios-jb
python3 selftest.py     # 不用手機也能跑：協議＋判讀的假資料測試（PC 上也可以）
python3 -m unittest discover -s tests -v  # HTTP／socket／取消與記帳的離線回歸測試
python3 probe.py        # 真的連 tweak，只讀畫面不點東西
```

`probe.py` 會印螢幕尺寸、整頁 OCR、任務框 OCR、直播主名稱、判讀結果，還有**「領取」那一格
單獨 OCR 到什麼**——那是「能不能自動領」的關鍵。把整段輸出貼回來就知道 `screens.py` 的框
要不要微調。

想抓「倒數變成領取」的那一瞬間（確認按鈕上的字真的讀得到）：

```sh
python3 probe.py --watch 120     # 盯任務框兩分鐘，文字有變就印一行
```

## 每天用

```sh
python3 bot.py run --dry-run    # 只讀畫面、不點不滑（第一次跑建議先用這個看它讀到什麼）
python3 bot.py run              # 前景跑（Ctrl-C 停）
python3 bot.py run --hours 3 --no-withdraw
python3 bot.py status           # 今天的統計
python3 bot.py panel            # 開一次任務面板（領觀看任務、視情況提領）
python3 bot.py look             # 印出目前畫面的判讀結果（除錯用）
python3 bot.py tap 330 500      # 手動點一下（point）
```

節奏的抖動跟其他版本一致（`jitter.py`）：等待 ±18%、面板間隔 ±25%、每個直播間的上限只往上
抽、倒數提前 1~6 秒醒、收尾時間每天固定在 23:35~23:50 之間、點擊座標 ±5 point
（`--tap-jitter` 可在 0～5 調整；超出這個範圍會拒絕執行，避免點出按鈕）。

停止檔、時間上限與 Ctrl-C 都會停止後續動作，不會在停止後再開面板提領。`--dry-run`
不會觸控、滑動、顯示 toast 或更新領取統計。統計與每日排程使用台灣時間；同一份狀態檔
同時只允許一個程序操作。若提領送出後結果不明，`withdrawal_pending` 會保留在
`state-ios.json`，直到你確認實際提領紀錄並修正該旗標前不會再自動提領。
讀不到最新餘額時會取消本次提領，不會使用歷史統計中的餘額。提領結果未知時仍可繼續
其他觀看任務，但不會再次自動送出提領；只有明確成功或明確拒絕才能解除未確認旗標。
無法辨認的畫面只會重新讀取，之後等待你切回直播間，不再嘗試固定座標的返回鍵。

建議用 `nohup python3 bot.py run >/dev/null 2>&1 &` 丟到背景，再切回蝦皮；這樣手機你還是
可以拿去用別的東西（但 bot 會搶著點畫面，跑的時候別用手機）。

## 一鍵開始

iOS 沒有任何「桌面小工具可以執行任意程式」的官方路徑，App Store 的 App 又會被系統凍結
（凍結了就沒辦法在蝦皮在前景時繼續跑）。所以最省事的做法是：**Safari 書籤（點一下）
＋ 背景的一支小程式**。

```sh
sh start.sh          # 開機後跑一次：把觸發器丟到背景（常駐 127.0.0.1:8080）
```

然後 Safari 開 `http://127.0.0.1:8080/` → 分享 → **加入主畫面**，命名成「開始領蝦幣」。
之後開啟圖示，再按頁面上的「開始」就會：

1. 把蝦皮叫回前景（tweak 的 task 11）
2. 約 2 秒後把 `bot.py run` 丟到背景開始跑
3. 網頁上有「停止」按鈕；等待期間約每 0.25 秒檢查停止，正在等待 socket 時會在逾時後停止。
   超過 90 秒仍未退出時，控制頁會終止這次啟動的程序。

`GET /start` 與 `GET /stop` 現在只顯示控制頁，不會操作手機。按鈕會使用帶驗證碼的 POST，
並檢查 Host／Origin，防止惡意網頁替你啟停本機 bot。原有 `/start` 書籤仍可開啟控制頁，
但需要再按一次「開始」。`sh stop.sh` 也會取消尚未完成的啟動，不會漏掉啟動中的兩秒空檔。
紀錄會輪替，網頁每次最多讀取尾端 64 KiB；HTTP 同時最多處理 8 個連線。

想連 `start.sh` 都不用跑，把 `com.autocoin.trigger.plist` 複製到
`/var/jb/Library/LaunchDaemons/`（改成你的路徑），開機就會自己起來：

```sh
cp com.autocoin.trigger.plist /var/jb/Library/LaunchDaemons/
launchctl load /var/jb/Library/LaunchDaemons/com.autocoin.trigger.plist
```

## 檔案

| 檔案 | 用途 |
|---|---|
| `zxtouch.py` | 協議客戶端（純標準庫）＋「這一版沒有的功能」明確報錯 |
| `screens.py` | 畫面判讀（OCR 版）：任務框、任務獎勵面板、提領頁 |
| `bot.py` | 主流程（跟 `ios/bot.py` 同一套邏輯） |
| `probe.py` | 手機上的先驗證（只讀畫面） |
| `jitter.py` | 等待與點擊的隨機抖動（跟其他版本同一套） |
| `selftest.py` / `tests/` | 不用手機的自我測試與安全回歸測試 |
| `serve.py` / `start.sh` / `stop.sh` | 一鍵開始的觸發器（127.0.0.1:8080） |
| `com.autocoin.trigger.plist` | 讓觸發器開機自動起來（可選） |

## 風險（要先知道）

- **jailbreak 本身就是可被偵測的特徵**（`/var/jb`、dyld image、URL scheme 探測…）。
  蝦皮的風控除了抓自動化，也可能因為裝了越獄而限制帳號。也就是說，這條路可能比
  TouchSynthesis 那條**更容易被盯上**（那條至少手機沒越獄）。要降低風險得另外處理越獄隱藏
  （Choicy / FlyJB X / Shadow 之類），但不保證擋得住。
- 蝦皮服務條款 6.2(k) 一樣禁止機器人；官方說偵測到風險活動會拒絕領取或讓按鈕消失。
- ZXTouch 是 root tweak，注入 SpringBoard；出問題（白蘋果、卡住）要能自己救回來。
- 這一版 tweak 沒有取色，所以「可以領了嗎」是靠 OCR 讀「領取」兩個字；如果 Vision 對那顆
  按鈕讀不好，就要靠倒數（`probe.py --watch` 會告訴你）。
- 抖動只是降低「行為規律性」這個特徵，抵不掉越獄本身。
