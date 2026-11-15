"""WBI 签名。

B 站的 WBI 用 ``img_key`` + ``sub_key`` 重排成 ``mixin_key``，再对
「按 key 排序后的查询串 + mixin_key」取 MD5 做 ``w_rid``。

原实现少了**参数值过滤**这一步（WBI 规范要求去掉 ``!'()*`` 这几个字符），
当前接口的参数恰好不包含它们所以没出过事，但拿去签别的接口就会崩。这里补上。
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from typing import Mapping

from ..constants import BILI_LIVE_URL, UA_DESKTOP
from ..logging_setup import get_logger
from ..utils.webhttp import get_json

log = get_logger(__name__)

NAV_ENDPOINT = "https://api.bilibili.com/x/web-interface/nav"

#: mixin_key 重排表（官方 64 位固定表）
_MIXIN_TABLE = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
)

#: WBI 要求剔除的字符
_BAD_CHARS = re.compile(r"[!'()*]")

CACHE_TTL = 600.0


class WBISigner:
    """带缓存的 WBI 签名器。拿不到 key 时降级为「不签名」，由调用方兜底。"""

    def __init__(self, ttl: float = CACHE_TTL, clock=time.time) -> None:
        self._ttl = ttl
        self._clock = clock
        self._mixin: str | None = None
        self._stamp = 0.0

    # ---------------- 取 key ----------------

    def mixin_key(self, cookie: str = "", force: bool = False) -> str | None:
        now = self._clock()
        if not force and self._mixin and now - self._stamp < self._ttl:
            return self._mixin
        nav = get_json(NAV_ENDPOINT, headers={
            "User-Agent": UA_DESKTOP,
            "Referer": BILI_LIVE_URL,
            "Cookie": cookie or "",
        }, timeout=8)
        try:
            wbi = ((nav or {}).get("data") or {}).get("wbi_img") or {}
            img_key = str(wbi.get("img_url", "")).rsplit("/", 1)[-1].split(".")[0]
            sub_key = str(wbi.get("sub_url", "")).rsplit("/", 1)[-1].split(".")[0]
        except AttributeError:
            return None
        if not img_key or not sub_key:
            return None
        raw = img_key + sub_key
        mixin = "".join(raw[i] for i in _MIXIN_TABLE if i < len(raw))[:32]
        self._mixin = mixin
        self._stamp = now
        return mixin

    # ---------------- 签名 ----------------

    def sign(self, url: str, params: Mapping[str, object], cookie: str = "",
             timestamp: int | None = None) -> str:
        """返回带 ``wts`` / ``w_rid`` 的完整 URL；拿不到 mixin key 时原样返回。"""
        mixin = self.mixin_key(cookie)
        if not mixin:
            log.debug("WBI mixin key 不可用，跳过签名")
            return url
        signed = {key: _sanitize(value) for key, value in params.items()}
        signed["wts"] = str(timestamp if timestamp is not None else int(self._clock()))
        query = urllib.parse.urlencode(sorted(signed.items()))
        digest = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
        return f"{url}?{query}&w_rid={digest}"

    def invalidate(self) -> None:
        self._mixin = None
        self._stamp = 0.0


def _sanitize(value: object) -> str:
    """WBI 规范：参数值里去掉 ``!'()*``。"""
    return _BAD_CHARS.sub("", str(value))
