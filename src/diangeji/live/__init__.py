"""直播层：B 站弹幕。

* ``protocol``  —— 弹幕服务器的二进制协议（包头、压缩包解包）
* ``wbi``       —— B 站 WBI 签名
* ``commands``  —— 弹幕文本 → 点歌指令
* ``client``    —— 连接、鉴权、心跳、重连的客户端

这一层只负责「拿到谁点了什么歌」，交给 :class:`callback`，不直接改播放状态。
"""

from __future__ import annotations

from .client import DanmakuClient, DanmakuState
from .commands import parse_order
from .wbi import WBISigner

__all__ = ["DanmakuClient", "DanmakuState", "parse_order", "WBISigner"]
