#!/bin/sh
# 在手機上跑一次，把「一鍵開始」的觸發器丟到背景。
# 之後點主畫面那顆 Safari 圖示就會開始領。
cd "$(dirname "$0")" || exit 1
python3 serve.py --start || exit 1
echo "Safari 開 http://127.0.0.1:8080/ → 分享 → 加入主畫面；開啟後按開始"
