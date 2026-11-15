"""音源协议。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..core.models import Track, TrackSource


@dataclass(slots=True)
class ResolveResult:
    """一次解析的结果。``track`` 为空表示没找到。"""

    track: Track | None
    source: TrackSource
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.track is not None and bool(self.track.path)

    @classmethod
    def miss(cls, source: TrackSource, detail: str = "") -> "ResolveResult":
        return cls(track=None, source=source, detail=detail)


@runtime_checkable
class MusicSource(Protocol):
    """音源接口。

    * ``name``      给日志和界面用。
    * ``enabled()`` 运行时判断是否可用（比如 QQ 音乐需要 cookie）。
    * ``search()``  返回候选列表，用于界面搜索。
    * ``resolve()`` 关键词 → 可直接播放的 ``Track``；找不到返回 ``None``。
    * ``refresh()`` 刷新过期直链（本地文件原样返回）。
    """

    name: str
    source: TrackSource

    def enabled(self) -> bool: ...

    def search(self, keyword: str, limit: int = 6) -> list[Track]: ...

    def resolve(self, keyword: str) -> Track | None: ...

    def refresh(self, track: Track) -> Track: ...
