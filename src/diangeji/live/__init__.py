"""直播层：B 站弹幕。

* ``protocol``  —— 弹幕服务器的二进制协议（包头、压缩包解包）
* ``wbi``       —— B 站 WBI 签名
* ``commands``  —— 弹幕文本 → 点歌指令
* ``client``    —— 连接、鉴权、心跳、重连的客户端

这一层只负责「拿到谁点了什么歌」，交给 :class:`callback`，不直接改播放状态。
"""

from __future__ import annotations

# 注意：**不要**在这里 eager import ``.client``。
# ``client.py`` 里写的是 ``from . import protocol``，如果本模块一上来就
# import client，图上就成 ``live → live.client → live`` 的环，而且代价是
# 任何 ``import diangeji.live`` 都要连带加载 client、utils.ws、utils.webhttp、
# paths 一整条链 —— 只想用 ``parse_order`` 的调用方白付这笔钱（约 400ms）。
#
# 需要客户端的代码请显式 ``from .live.client import DanmakuClient``。

from .commands import parse_order

__all__ = ["parse_order"]


def __getattr__(name: str):
    """按需取 ``DanmakuClient`` / ``DanmakuState``（PEP 562）。

    这样 ``from diangeji.live import DanmakuClient`` 仍然能用，
    但只有真的去取时才会加载 ``client``。
    """
    if name in ("DanmakuClient", "DanmakuState"):
        from .client import DanmakuClient as _Client, DanmakuState as _State
        return {"DanmakuClient": _Client, "DanmakuState": _State}[name]
    if name == "WBISigner":
        from .wbi import WBISigner as _Signer
        return _Signer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
