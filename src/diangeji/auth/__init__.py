"""认证层：扫码登录与登录态持久化。

* ``store``    —— cookie 文件读写（原子替换，别写坏）
* ``qrcode_login`` —— B 站 / QQ 音乐扫码
* ``browser``  —— 浏览器登录兜底（CDP 取 cookie）

注意：cookie 是**明文凭据**。这一层只负责存取，不外发、不打印、不进日志。
"""

from __future__ import annotations

from .browser import BrowserLogin, CDPClient, clean_env, find_edge
from .qrcode_login import BiliQRLogin, LoginResult, QQMusicQRLogin, QRPayload
from .store import CookieStore

__all__ = [
    "BrowserLogin",
    "CDPClient",
    "clean_env",
    "find_edge",
    "BiliQRLogin",
    "LoginResult",
    "QQMusicQRLogin",
    "QRPayload",
    "CookieStore",
]
