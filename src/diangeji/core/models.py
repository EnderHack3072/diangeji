"""领域模型：曲目与队列项。

字段与前端 JSON 保持一致（``index.html`` / ``overlay.html`` 直接读这些键），
所以改字段名等于改接口 —— 需要同时改模板。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from ..utils.timeutil import now_ts


class TrackSource(str, Enum):
    """音源标识。字符串枚举，序列化后就是 ``"local"`` / ``"qq"``。"""

    LOCAL = "local"
    QQ = "qq"
    NONE = "none"

    @classmethod
    def of(cls, value: Any) -> "TrackSource":
        try:
            return cls(str(value))
        except ValueError:
            return cls.NONE

    @property
    def label(self) -> str:
        return {TrackSource.LOCAL: "本地曲库", TrackSource.QQ: "QQ 音乐"}.get(self, "未知来源")


class PlaybackPhase(str, Enum):
    """播放状态机。只用于内部判断，不直接暴露给前端。"""

    IDLE = "idle"          # 没有在播
    PLAYING = "playing"    # 正常播放
    PAUSED = "paused"      # 暂停


def new_id() -> str:
    """队列项 ID：8 位十六进制，够用且短。"""
    return uuid.uuid4().hex[:8]


@dataclass(slots=True)
class Track:
    """一首被解析出来的曲目（还没进队列）。"""

    title: str
    path: str | None = None
    mid: str | None = None
    source: TrackSource = TrackSource.NONE
    singer: str = ""
    album: str = ""

    @property
    def playable(self) -> bool:
        """有地址才算能播。"""
        return bool(self.path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "path": self.path,
            "mid": self.mid,
            "source": self.source.value,
            "singer": self.singer,
            "album": self.album,
        }


@dataclass(slots=True)
class QueueItem:
    """队列里的一条点歌 / 正在播放的那一条。"""

    keyword: str
    title: str
    user: str = "观众"
    path: str | None = None
    source: TrackSource = TrackSource.NONE
    mid: str | None = None
    id: str = field(default_factory=new_id)
    added: int = field(default_factory=now_ts)
    started: int | None = None
    idle: bool = False

    @property
    def playable(self) -> bool:
        return bool(self.path)

    def with_playback_started(self) -> "QueueItem":
        """标记为「现在开始播」。

        ``idle`` 标记**必须保留**：它有两个作用 ——
        前端显示「空闲随机」，以及 :meth:`PlayQueue.push` 认出当前是随机播放
        好让观众的点歌直接顶掉它。清掉的话随机歌会一直占着播放位。
        """
        return replace(self, started=now_ts())

    def with_path(self, path: str | None) -> "QueueItem":
        """刷新直链（QQ 音乐地址二十分钟过期）。"""
        return replace(self, path=path)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "user": self.user,
            "keyword": self.keyword,
            "title": self.title,
            "path": self.path,
            "source": self.source.value,
            "mid": self.mid,
            "added": self.added,
        }
        if self.started is not None:
            data["started"] = self.started
        if self.idle:
            data["idle"] = True
        return data

    # ---- 构造 ----

    @classmethod
    def from_track(cls, track: Track, keyword: str, user: str = "观众") -> "QueueItem":
        return cls(
            keyword=keyword,
            title=track.title or keyword,
            user=(user or "观众")[:16],
            path=track.path,
            source=track.source,
            mid=track.mid,
        )

    def copy_as_detached(self, user: str = "随机", idle: bool = True) -> "QueueItem":
        """空闲随机播放用的副本。"""
        return replace(self, id=new_id(), user=user, idle=idle, added=now_ts())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueueItem":
        """容错构造（主要给测试和旧数据用）。"""
        return cls(
            keyword=str(data.get("keyword") or data.get("title") or ""),
            title=str(data.get("title") or data.get("keyword") or ""),
            user=str(data.get("user") or "观众"),
            path=data.get("path") or None,
            source=TrackSource.of(data.get("source")),
            mid=data.get("mid") or None,
            id=str(data.get("id") or new_id()),
            added=int(data.get("added") or now_ts()),
            started=data.get("started"),
            idle=bool(data.get("idle")),
        )
