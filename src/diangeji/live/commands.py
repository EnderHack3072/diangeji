"""弹幕文本 → 点歌指令。

从 ``danmaku.py`` 里抠出来的纯函数，方便单测：给一句话，回答「是不是点歌，点的什么」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..constants import ORDER_PREFIXES

#: 「点歌 晴天」/「点播：晴天」/「来一首,晴天」
_ORDER_RE = re.compile(
    r"^(?:" + "|".join(map(re.escape, ORDER_PREFIXES)) + r")\s*[:：,，、]?\s*(.+)$"
)


@dataclass(slots=True)
class Order:
    """一条解析成功的点歌指令。"""

    keyword: str
    raw_text: str


def parse_order(text: str) -> Order | None:
    """解析点歌指令；不是点歌返回 ``None``。

    容忍：前后空格、全角/半角冒号与逗号、感叹号等结尾标点。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    match = _ORDER_RE.match(raw)
    if not match:
        return None
    keyword = match.group(1).strip(" :：,，、。.!！~～\t")
    if not keyword:
        return None
    # 长到离谱的「歌名」多半是刷屏，直接丢
    if len(keyword) > 60:
        return None
    return Order(keyword=keyword, raw_text=raw)


def is_order(text: str) -> bool:
    return parse_order(text) is not None
