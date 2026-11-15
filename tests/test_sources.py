"""音源与播放编排：本地优先、QQ 掉线降级、空闲随机、直链刷新。"""

from __future__ import annotations

import pytest

from diangeji.core.models import QueueItem, Track, TrackSource
from diangeji.core.queue import PlayQueue
from diangeji.services.playback import PlaybackService
from diangeji.sources.base import ResolveResult
from diangeji.sources.local_source import LocalSource
from diangeji.sources.registry import SourceRegistry, default_registry

from conftest import FakeSource


# ---------------------------------------------------------------- 本地音源

class TestLocalSource:
    def test_always_enabled(self, library):
        assert LocalSource(library).enabled() is True

    def test_resolve_hit(self, library):
        track = LocalSource(library).resolve("晴天")
        assert track is not None
        assert track.source is TrackSource.LOCAL
        assert track.path and track.path.endswith("晴天.mp3")

    def test_resolve_miss(self, library):
        assert LocalSource(library).resolve("没有这首") is None

    def test_refresh_returns_same(self, library):
        source = LocalSource(library)
        track = source.resolve("晴天")
        assert source.refresh(track).path == track.path

    def test_search_returns_tracks(self, library):
        hits = LocalSource(library).search("晴天")
        assert hits and all(isinstance(t, Track) for t in hits)


# ---------------------------------------------------------------- 注册表

class TestRegistry:
    def test_local_preferred(self, library):
        """本地有就不该去网上找 —— 省流量也更快。"""
        fake = FakeSource({"晴天": "https://example.test/x.mp3"})
        registry = SourceRegistry([LocalSource(library), fake])
        result = registry.resolve("晴天")
        assert result.track is not None
        assert result.track.source is TrackSource.LOCAL
        assert fake.calls == []

    def test_falls_back_to_remote(self, library):
        fake = FakeSource({"遥远的她": "https://example.test/x.mp3"})
        registry = SourceRegistry([LocalSource(library), fake])
        result = registry.resolve("遥远的她")
        assert result.track is not None
        assert result.track.source is TrackSource.QQ
        assert fake.calls == ["遥远的她"]

    def test_disabled_source_skipped(self, library):
        fake = FakeSource({}, enabled=False)
        registry = SourceRegistry([LocalSource(library), fake])
        result = registry.resolve("不存在")
        assert result.track is None
        assert fake.calls == []

    def test_miss_everywhere(self, library):
        registry = SourceRegistry([LocalSource(library), FakeSource({})])
        assert registry.resolve("完全不存在").track is None

    def test_prefer_flag(self, library):
        fake = FakeSource({"晴天": "https://example.test/x.mp3"})
        registry = SourceRegistry([LocalSource(library), fake])
        result = registry.resolve("晴天", prefer=TrackSource.QQ)
        assert result.track is not None
        assert result.track.source is TrackSource.QQ

    def test_refresh_goes_to_owner(self, library):
        fake = FakeSource({"晴天": "https://example.test/new.mp3"})
        registry = SourceRegistry([LocalSource(library), fake])
        remote = Track(title="晴天", path="https://old", mid="晴天", source=TrackSource.QQ)
        refreshed = registry.refresh(remote)
        assert refreshed is not None and refreshed.path == "https://example.test/new.mp3"

    def test_refresh_skips_local(self, library):
        registry = SourceRegistry([LocalSource(library)])
        local = Track(title="晴天", path="/a.mp3", source=TrackSource.LOCAL)
        assert registry.refresh(local).path == "/a.mp3"

    def test_refresh_unknown_source_is_noop(self, library):
        registry = SourceRegistry([LocalSource(library)])
        track = Track(title="x", path="", source=TrackSource.NONE)
        assert registry.refresh(track).path == ""

    def test_search_all_merges(self, library):
        fake = FakeSource({"独一无二": "https://example.test/u.mp3"})
        registry = SourceRegistry([LocalSource(library), fake])
        titles = [t.title for t in registry.search_all("晴天")]
        assert "晴天" in titles

    def test_enabled_sources(self, library):
        fake = FakeSource({}, enabled=False)
        names = [s.name for s in SourceRegistry([LocalSource(library), fake]).enabled_sources()]
        assert fake.name not in names

    def test_default_registry_is_local_first(self, library):
        fake = FakeSource({})
        registry = default_registry(LocalSource(library), fake)
        assert registry.sources[0].name == LocalSource.name


# ---------------------------------------------------------------- 播放编排

class TestPlayback:
    def test_order_resolves_and_plays(self, playback):
        item = playback.request("小明", "晴天")
        assert item.path and item.user == "小明"
        assert playback.snapshot()["playing"]["title"] == "晴天"

    def test_empty_keyword_raises(self, playback):
        with pytest.raises(ValueError):
            playback.request("小明", "   ")

    def test_unresolved_still_queued(self, playback):
        """解析不到也入队：不能让观众的点歌凭空消失。"""
        item = playback.request("小明", "不存在的歌")
        assert item.path is None
        state = playback.snapshot()
        assert state["playing"] is None
        assert state["queue"][0]["title"] == "不存在的歌"

    def test_next_advances(self, playback):
        first = playback.request("a", "晴天")
        second = playback.request("b", "晴天")
        playing_a = playback.snapshot()["playing"]["id"]
        playback.next()
        playing_b = playback.snapshot()["playing"]["id"]
        # 两首同源曲目 path 相同，只有 id 能证明真的换了
        assert playing_a != playing_b
        assert playing_b in (first.id, second.id)

    def test_skip_if_finished_matches(self, playback):
        item = playback.request("a", "晴天")
        playing = playback.snapshot()["playing"]
        assert playback.skip_if_finished(playing["id"]) is True
        assert playback.skip_if_finished(playing["id"]) is False

    def test_skip_if_finished_wrong_id(self, playback):
        playback.request("a", "晴天")
        assert playback.skip_if_finished("deadbeef") is False
        assert playback.snapshot()["playing"] is not None

    def test_skip_if_finished_nothing_playing(self, playback):
        assert playback.skip_if_finished("anything") is False

    def test_previous(self, playback):
        playback.request("a", "晴天")
        playback.request("b", "晴天")
        playback.next()
        state = playback.previous()
        assert state["playing"] is not None

    def test_volume(self, playback):
        assert playback.set_volume(70)["volume"] == 70

    def test_pause(self, playback):
        playback.request("a", "晴天")
        assert playback.set_paused(True)["paused"] is True
        assert playback.set_paused(False)["paused"] is False

    def test_remove_and_clear(self, playback):
        playback.request("a", "晴天")
        queued = playback.request("b", "晴天")
        playback.remove(queued.id)
        assert playback.snapshot()["queue"] == []
        playback.clear()
        assert playback.snapshot()["playing"] is None


class TestIdle:
    """空闲随机：队列空了自动补歌，有人点歌要能顶掉。"""

    def build(self, library, pool=("晴天", "晴天娃娃")):
        registry = SourceRegistry([LocalSource(library)])
        return PlaybackService(queue=PlayQueue(idle=True), registry=registry,
                               idle_pool=list(pool))

    def test_idle_fills_empty_queue(self, library):
        service = self.build(library)
        service.set_idle(True)
        playing = service.snapshot()["playing"]
        assert playing is not None
        assert playing.get("idle") is True

    def test_idle_respects_pool_misses(self, library):
        """候选全解析不出来时不该塞进不可播的项。"""
        service = self.build(library, pool=["完全不存在", "也不存在"])
        service.set_idle(True)
        assert service.snapshot()["playing"] is None

    def test_idle_disabled_does_nothing(self, library):
        registry = SourceRegistry([LocalSource(library)])
        service = PlaybackService(queue=PlayQueue(idle=False), registry=registry,
                                  idle_pool=["晴天"])
        service.set_idle(False)
        assert service.snapshot()["playing"] is None

    def test_real_order_interrupts_idle(self, library):
        service = self.build(library)
        service.set_idle(True)
        assert service.snapshot()["playing"].get("idle") is True
        service.request("小明", "晴天")
        playing = service.snapshot()["playing"]
        assert playing is not None
        assert not playing.get("idle")
        assert playing["user"] == "小明"

    def test_idle_after_queue_drains(self, library):
        service = self.build(library)
        service.request("小明", "晴天")
        service.next()
        assert service.snapshot()["playing"] is not None

    def test_idle_pool_override(self, library):
        service = self.build(library)
        service.set_idle_pool(["晴天娃娃"])
        service.set_idle(True)
        playing = service.snapshot()["playing"]
        assert playing is not None
        assert "晴天娃娃" in playing["title"]


class TestRefreshOnAdvance:
    def test_refresh_called_outside_lock(self, library):
        """刷新在锁外跑：回调里访问队列不能死锁。"""
        holder = {}

        class SlowSource(FakeSource):
            def refresh(self, track):
                holder["state"] = holder["service"].snapshot()
                return track

        service = PlaybackService(
            queue=PlayQueue(idle=False),
            registry=SourceRegistry([LocalSource(library), SlowSource({"遥远": "https://e.test/1.mp3"})]))
        holder["service"] = service
        service.request("a", "晴天")
        service.request("b", "遥远")   # QQ 曲目才会走 SlowSource.refresh
        service.next()
        assert "state" in holder


class TestResolveResult:
    def test_ok(self):
        result = ResolveResult(track=Track(title="x", path="/a.mp3"),
                               source=TrackSource.LOCAL)
        assert result.ok is True

    def test_empty(self):
        assert ResolveResult.miss(TrackSource.QQ).track is None
        assert ResolveResult.miss(TrackSource.QQ).ok is False


# ---------------------------------------------------------------- 曲库服务

class TestLibraryService:
    def test_scan_counts(self, library, data_dir):
        from diangeji.services.library_service import LibraryService
        service = LibraryService(library, data_dir)
        count = service.scan()
        assert count == len(library)
        assert count >= 3

    def test_scan_extra_dirs(self, library, data_dir, audio_tree):
        from diangeji.services.library_service import LibraryService
        service = LibraryService(library, data_dir)
        assert service.scan([str(audio_tree)]) >= 3

    def test_scan_missing_dir_survives(self, library, data_dir, tmp_path):
        from diangeji.services.library_service import LibraryService
        service = LibraryService(library, data_dir)
        assert service.scan([str(tmp_path / "nope")]) >= 0

    def test_default_music_dirs_returns_paths(self, data_dir):
        from diangeji.services.library_service import default_music_dirs
        assert isinstance(default_music_dirs(data_dir), list)
