"""测试夹具：临时数据目录 + 不联网的假音源。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from diangeji.core.library import LocalLibrary  # noqa: E402
from diangeji.core.models import Track, TrackSource  # noqa: E402
from diangeji.core.queue import PlayQueue  # noqa: E402
from diangeji.services.playback import PlaybackService  # noqa: E402
from diangeji.sources.base import MusicSource  # noqa: E402
from diangeji.sources.local_source import LocalSource  # noqa: E402
from diangeji.sources.registry import SourceRegistry  # noqa: E402


class FakeSource(MusicSource):
    """固定回答的假音源，记录调用次数。"""

    name = "假音源"
    source = TrackSource.QQ

    def __init__(self, table: dict[str, str] | None = None, enabled: bool = True) -> None:
        self.table = dict(table or {})
        self._enabled = enabled
        self.calls: list[str] = []

    def enabled(self) -> bool:
        return self._enabled

    def search(self, keyword: str, limit: int = 6) -> list[Track]:
        return [self.resolve(keyword)] if keyword in self.table else []

    def resolve(self, keyword: str) -> Track | None:
        self.calls.append(keyword)
        url = self.table.get(keyword)
        if not url:
            return None
        return Track(title=keyword, path=url, mid=keyword, source=self.source)

    def refresh(self, track: Track) -> Track:
        return Track(title=track.title, path=self.table.get(track.mid or "", track.path),
                     mid=track.mid, source=self.source)


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "点歌姬数据"
    directory.mkdir()
    return directory


@pytest.fixture()
def audio_tree(tmp_path: Path) -> Path:
    """造几个够大的假音频文件（content 大小超过 300KB 门槛）。"""
    root = tmp_path / "music"
    (root / "album").mkdir(parents=True)
    big = b"\0" * (301 * 1024)
    for name in ("晴天.mp3", "晴天娃娃.mp3", "album/成都.flac", "tiny.mp3"):
        path = root / name
        path.write_bytes(big if name != "tiny.mp3" else b"x")
    return root


@pytest.fixture()
def library(audio_tree: Path) -> LocalLibrary:
    lib = LocalLibrary()
    lib.scan([str(audio_tree)])
    return lib


@pytest.fixture()
def playback(library: LocalLibrary) -> PlaybackService:
    registry = SourceRegistry([
        LocalSource(library),
        FakeSource({"晴天": "https://example.test/qing.mp3"}),
    ])
    return PlaybackService(
        queue=PlayQueue(volume=30, idle=False),
        registry=registry,
        idle_pool=["周深 大鱼", "赵雷 成都"],
    )
