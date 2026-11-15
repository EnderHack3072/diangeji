"""领域层：不依赖 Tkinter / HTTP / 网络，纯 Python 可测。

这里放**业务对象**和**业务规则**：

* ``models``   —— ``Track`` / ``QueueItem``
* ``library``  —— 本地曲库扫描与模糊匹配
* ``queue``    —— 播放队列状态机（原 ``server.py`` 里那堆全局变量）
* ``errors``   —— 领域异常

这一层不认识 HTTP，也不认识界面。上层（services / web / ui）只能调用它。
"""

from __future__ import annotations

from .errors import (
    DianGeJiError,
    DuplicateTrackError,
    EmptyKeywordError,
    LibraryUnavailableError,
    NoPlayableTrackError,
    TrackNotFoundError,
)
from .library import LocalLibrary
from .models import PlaybackPhase, QueueItem, Track, TrackSource
from .queue import PlayQueue

__all__ = [
    "DianGeJiError",
    "DuplicateTrackError",
    "EmptyKeywordError",
    "LibraryUnavailableError",
    "NoPlayableTrackError",
    "TrackNotFoundError",
    "LocalLibrary",
    "PlaybackPhase",
    "QueueItem",
    "Track",
    "TrackSource",
    "PlayQueue",
]
