#!/bin/sh
# 停止正在跑的 bot（觸發器會自己收尾）
cd "$(dirname "$0")" || exit 1
exec python3 serve.py --stop
