"""音源注册表：按优先级依次尝试。

顺序可调（以后想做「全用 QQ 音乐」就换一下 ``sources``），
并且**把不可用的音源跳过**（没登录 QQ 音乐时不会白等 12 秒超时）。
"""

from __future__ import annotations

from typing import Iterable

from ..core.models import Track, TrackSource
from ..logging_setup import get_logger
from .base import MusicSource, ResolveResult

log = get_logger(__name__)


class SourceRegistry:
    """一组音源，按顺序解析。"""

    def __init__(self, sources: Iterable[MusicSource] | None = None) -> None:
        self.sources: list[MusicSource] = list(sources or [])

    def add(self, source: MusicSource) -> "SourceRegistry":
        self.sources.append(source)
        return self

    def get(self, source: TrackSource) -> MusicSource | None:
        for item in self.sources:
            if item.source is source:
                return item
        return None

    def enabled_sources(self) -> list[MusicSource]:
        """当前真正可用的音源。``enabled()`` 抛异常一律视为不可用。"""
        result = []
        for item in self.sources:
            try:
                if item.enabled():
                    result.append(item)
            except Exception as exc:  # pragma: no cover - 防御
                log.warning("音源 %s 状态检查失败：%s", item.name, exc)
        return result

    def resolve(self, keyword: str, *, prefer: TrackSource | None = None) -> ResolveResult:
        """按优先级解析关键词。``prefer`` 指定的音源排在最前。"""
        key = (keyword or "").strip()
        if not key:
            return ResolveResult.miss(TrackSource.NONE, "关键词为空")

        ordered = self.enabled_sources()
        if prefer is not None:
            ordered.sort(key=lambda s: 0 if s.source is prefer else 1)

        for source in ordered:
            try:
                track = source.resolve(key)
            except Exception as exc:
                log.warning("音源 %s 解析 %r 失败：%s", source.name, key, exc)
                continue
            if track is not None and track.playable:
                return ResolveResult(track=track, source=source.source)
        return ResolveResult.miss(TrackSource.NONE, f"没搜到 {key}")

    def refresh(self, track: Track | None) -> Track | None:
        """播放前刷新直链。找不到对应音源就原样返回。"""
        if track is None or not track.playable:
            return track
        source = self.get(track.source)
        if source is None:
            return track
        try:
            return source.refresh(track)
        except Exception as exc:  # pragma: no cover - 刷新失败沿用旧地址
            log.warning("刷新 %s 直链失败：%s", track.title, exc)
            return track

    def search_all(self, keyword: str, limit: int = 6) -> list[Track]:
        """跨音源搜索（界面用）。"""
        key = (keyword or "").strip()
        if not key:
            return []
        out: list[Track] = []
        for source in self.enabled_sources():
            try:
                out.extend(source.search(key, limit=limit))
            except Exception as exc:  # pragma: no cover
                log.debug("音源 %s 搜索失败：%s", source.name, exc)
        return out


def default_registry(local: MusicSource, qq: MusicSource) -> SourceRegistry:
    """默认顺序：本地曲库优先，找不到才走 QQ 音乐。"""
    return SourceRegistry([local, qq])
