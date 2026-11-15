"""时间小工具。"""

from __future__ import annotations

import time


def now_ts() -> int:
    """当前 Unix 秒（整数，前端 ``new Date(ts*1000)`` 直接用）。"""
    return int(time.time())


def to_clock(ts: float) -> str:
    """Unix 秒 → ``HH:MM:SS``。"""
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "--:--:--"
