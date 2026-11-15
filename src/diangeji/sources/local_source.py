"""本地曲库音源：包一层 ``core.library.LocalLibrary``，让队列不关心它是本地还是在线。"""

from __future__ import annotations

from ..core.library import LocalLibrary
from ..core.models import Track, TrackSource
from .base import MusicSource


class LocalSource(MusicSource):
    """本地文件。永远是「已启用」，刷新直链是空操作。"""

    name = "本地曲库"
    source = TrackSource.LOCAL

    def __init__(self, library: LocalLibrary) -> None:
        self.library = library

    def enabled(self) -> bool:
        return True

    def search(self, keyword: str, limit: int = 6) -> list[Track]:
        return [self._to_track(hit) for hit in self.library.search(keyword, limit=limit)]

    def resolve(self, keyword: str) -> Track | None:
        hit = self.library.find(keyword)
        return self._to_track(hit) if hit else None

    def refresh(self, track: Track) -> Track:
        return track

    @staticmethod
    def _to_track(hit: dict[str, object]) -> Track:
        return Track(
            title=str(hit.get("title") or ""),
            path=str(hit["path"]) if hit.get("path") else None,
            source=TrackSource.LOCAL,
        )
