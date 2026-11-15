"""Web 层：本地 HTTP 服务（主播台 / OBS 覆盖层 / API / 媒体）。

* ``security`` —— 同源校验（原 ``_local_ok``，本轮改成默认拒绝）
* ``routes``   —— 路由表与处理器
* ``server``   —— ThreadingHTTPServer 的组装与生命周期
* ``views``    —— 模板渲染（把状态注入 HTML 时做转义）

这一层只做「HTTP ↔ 服务调用」，不含业务逻辑，也不碰 tkinter。
"""

from __future__ import annotations

from .security import is_local_request, parse_host, parse_origin
from .server import HttpService
from .views import render_console, render_overlay, render_state_script

__all__ = [
    "is_local_request",
    "parse_host",
    "parse_origin",
    "HttpService",
    "render_console",
    "render_overlay",
    "render_state_script",
]
