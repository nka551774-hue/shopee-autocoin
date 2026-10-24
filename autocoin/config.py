"""設定值，可以用命令列參數覆蓋一部分。"""
from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass
class Config:
    serial: str = os.environ.get("AUTOCOIN_SERIAL", "")  # 未指定時僅接受唯一一台 ADB 裝置
    adb_host: str = "127.0.0.1"
    adb_port: int = 5037

    # 挑直播間
    min_room_coins: float = 0.5      # 卡片上「最多X」低於這個值就不考慮
    explore_pages: int = 6           # 「看更多」最多往下捲幾次
    # 待在直播間
    max_room_minutes: float = 75     # 保險：單一直播間最多待多久
    no_coin_grace: float = 60        # 「直播間蝦幣」不見多久就換台（領完、回到直播間時會暫時消失）
    watch_room_minutes: float = 10.5 # 只為觀看任務待的話，每間待多久（每場只算 10 分鐘）

    # 任務獎勵面板
    panel_every_minutes: float = 30  # 多久開一次面板，把觀看任務的蝦幣領進待提領
    withdraw: bool = True            # 自動提領
    withdraw_min: float = 5.0        # 待提領至少這麼多才提領；每天最後一次面板會提領剩下的（≥1）
    withdraw_daily_limit: int = 3

    # 節奏抖動：等待時間與點擊座標都加隨機擾動，避免固定週期變成風控特徵
    jitter: float = 0.18            # 等待時間 ±18%
    tap_jitter_px: int = 9          # 點擊座標 ±9 px

    state_path: str = "state.json"
    log_dir: str = "logs"
