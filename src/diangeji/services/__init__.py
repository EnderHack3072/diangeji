"""应用服务层：把领域对象、音源、弹幕、HTTP 串起来。

这一层可以有 I/O，但**不碰界面**：界面在 ``ui``，HTTP 在 ``web``，
两边都只调用这里的服务对象。

* ``playback``  播放编排（点歌 → 解析 → 入队 → 切歌 → 空闲随机）
* ``browser``   浏览器自动化（启动 Edge、CDP 取 cookie）
* ``library_service`` 曲库扫描的组装
"""

from __future__ import annotations

from .library_service import LibraryService
from .playback import PlaybackService

__all__ = ["LibraryService", "PlaybackService"]
