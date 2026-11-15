"""本地服务的安全校验。

原 ``_local_ok()`` 的思路是对的（只绑 127.0.0.1 + 校验 Host/Origin/Referer），
但判据是「两个头都不存在就放行」，等于对本机任意进程全开。这里改成**默认拒绝**：

* ``Host`` 缺失或不解析成回环地址 → 拒绝（挡住 DNS rebinding）。
* 有 ``Origin`` / ``Referer`` 时必须解析成回环地址。
* 两个都没有（非浏览器客户端，如 curl、QQ 机器人转发）→ 拒绝，
  除非调用方显式传 ``allow_headless=True``（本机脚本调试用）。

这样「挂着直播时别的网页偷偷清队列」和「本机随便一个程序改状态」都被挡住。
"""

from __future__ import annotations

import urllib.parse
from email.message import Message
from typing import Mapping

from ..constants import LOCAL_HOSTS

#: 回环地址集合（含 IPv6）
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def parse_host(value: str | None) -> str:
    """把 ``127.0.0.1:8770`` / ``localhost`` 解析成主机名。"""
    if not value:
        return ""
    try:
        # 「::1:8770」这种没法直接 urlparse，先处理方括号形式
        candidate = value.strip()
        if candidate.startswith("["):
            return candidate.split("]")[0].lstrip("[") or ""
        host = urllib.parse.urlsplit(f"//{candidate}").hostname or ""
        return host.lower()
    except ValueError:
        return ""


def parse_origin(value: str | None) -> str:
    """把 ``http://127.0.0.1:8770`` 解析成主机名。"""
    if not value:
        return ""
    try:
        return (urllib.parse.urlsplit(value).hostname or "").lower()
    except ValueError:
        return ""


def is_loopback(host: str) -> bool:
    return host in LOCAL_HOSTS or host in LOOPBACK


def is_local_request(headers: Mapping[str, str] | Message,
                     allow_headless: bool = False) -> bool:
    """判断一个请求是否来自本机页面。

    ``headers`` 可以是 dict，也可以直接传 ``BaseHTTPRequestHandler.headers``。
    """
    def get(name: str) -> str:
        value = headers.get(name) if hasattr(headers, "get") else None
        return str(value) if value else ""

    host = parse_host(get("Host"))
    if not host or not is_loopback(host):
        return False

    seen_browser_header = False
    for name in ("Origin", "Referer"):
        raw = get(name)
        if not raw:
            continue
        seen_browser_header = True
        origin_host = parse_origin(raw)
        if not origin_host or not is_loopback(origin_host):
            return False

    if not seen_browser_header:
        # 非浏览器客户端：默认拒绝，需要调用方显式放开
        return allow_headless
    return True
