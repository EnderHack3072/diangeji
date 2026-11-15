"""音源层：把「去哪找歌」抽象成可替换的实现。

``base.MusicSource`` 是协议，``LocalSource`` / ``QQMusicSource`` 是两种实现，
``registry`` 负责按优先级串起来。上层只认接口，不认具体平台。

新增音源（网易云 / 酷狗）只要：实现 ``MusicSource`` → 在 ``registry`` 里注册，
播放编排、HTTP 接口、界面都不用动。
"""

from __future__ import annotations

from .base import MusicSource, ResolveResult
from .local_source import LocalSource
from .qqmusic import QQMusicConfig, QQMusicSource
from .registry import SourceRegistry, default_registry

__all__ = [
    "MusicSource",
    "ResolveResult",
    "LocalSource",
    "QQMusicSource",
    "QQMusicConfig",
    "SourceRegistry",
    "default_registry",
]
