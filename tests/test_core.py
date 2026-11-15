"""领域层：曲库匹配、队列状态机。这两块原先是全局变量，没法测。"""

from __future__ import annotations

from diangeji.core.models import QueueItem, Track, TrackSource
from diangeji.core.queue import PlayQueue


# ---------------------------------------------------------------- 曲库

class TestLibrary:
    def test_recursive_scan(self, library):
        # 子目录里的 成都.flac 也要扫到（原实现只扫一层）
        titles = {str(t["title"]) for t in library}
        assert "成都" in titles

    def test_skips_tiny_files(self, library):
        assert all("tiny" not in str(t["title"]) for t in library)

    def test_exact_match_beats_substring(self, library):
        """点「晴天」应该拿 晴天，不是 晴天娃娃。"""
        hit = library.find("晴天")
        assert hit is not None
        assert hit["title"] == "晴天"

    def test_prefix_match(self, library):
        hit = library.find("晴天娃")
        assert hit is not None
        assert hit["title"] == "晴天娃娃"

    def test_miss_returns_none(self, library):
        assert library.find("不存在的歌") is None

    def test_empty_keyword_returns_none(self, library):
        assert library.find("") is None
        assert library.find("   ") is None

    def test_path_for_rejects_empty(self, library):
        """原实现 endswith('') 恒真，GET /media/ 会把第一首吐出来。"""
        assert library.path_for("") is None

    def test_path_for_rejects_bare_substring(self, library):
        """``e.mp3`` 不能命中 ``name.mp3``。"""
        assert library.path_for("e.mp3") is None

    def test_path_for_exact_name(self, library):
        got = library.path_for("晴天.mp3")
        assert got is not None and got.endswith("晴天.mp3")

    def test_path_for_nested(self, library):
        assert library.path_for("album/成都.flac") is not None

    def test_search_filters(self, library):
        hits = library.search("晴天")
        assert len(hits) == 2


# ---------------------------------------------------------------- 队列

class TestPlayQueue:
    def test_first_order_starts_playing(self):
        queue = PlayQueue(idle=False)
        item = QueueItem(keyword="晴天", title="晴天", path="/tmp/a.mp3",
                         source=TrackSource.LOCAL)
        assert queue.push(item) is True
        assert queue.playing is not None
        assert queue.playing.title == "晴天"

    def test_second_order_queues(self):
        queue = PlayQueue(idle=False)
        queue.push(QueueItem(keyword="a", title="a", path="/a.mp3"))
        queue.push(QueueItem(keyword="b", title="b", path="/b.mp3"))
        assert queue.size == 1
        assert queue.snapshot()["queue"][0]["title"] == "b"

    def test_advance_moves_to_history(self):
        queue = PlayQueue(idle=False)
        queue.push(QueueItem(keyword="a", title="a", path="/a.mp3"))
        queue.push(QueueItem(keyword="b", title="b", path="/b.mp3"))
        state = queue.advance()
        assert state["playing"]["title"] == "b"
        assert state["history"][-1]["title"] == "a"

    def test_advance_skips_unplayable(self):
        """缺音源的条目在推进时被丢弃，而不是卡住播放位。"""
        queue = PlayQueue(idle=False)
        queue.push(QueueItem(keyword="a", title="a", path="/a.mp3"))
        queue.push(QueueItem(keyword="死歌", title="死歌", path=None))
        state = queue.advance()
        assert state["playing"] is None
        assert queue.size == 0

    def test_advance_runs_refresh_outside_lock(self):
        """刷新回调在锁外跑：回调里再访问队列不会死锁。"""
        queue = PlayQueue(idle=False)
        queue.push(QueueItem(keyword="a", title="a", path="/a.mp3"))
        queue.push(QueueItem(keyword="b", title="b", path="/b.mp3"))

        def refresh(item):
            _ = queue.snapshot()          # 能进来说明没持锁
            return item.with_path("/fresh.mp3")

        state = queue.advance(refresh=refresh)
        assert state["playing"]["path"] == "/fresh.mp3"

    def test_snapshot_can_omit_log(self):
        """轮询路径不该每次都深拷贝 40 条日志。"""
        queue = PlayQueue(idle=False)
        queue.push(QueueItem(keyword="a", title="a", path="/a.mp3"))
        queue.note("hello")

        full = queue.snapshot()
        light = queue.snapshot(include_log=False)
        assert "log" in full and full["log"]
        assert "log" not in light, "轻量快照不该带日志"

    def test_snapshot_can_limit_queue(self):
        queue = PlayQueue(idle=False)
        for i in range(80):
            queue.push(QueueItem(keyword=f"k{i}", title=f"t{i}", path=f"/{i}.mp3"))
        # 第一首直接顶上播放位，队列里剩 79 条
        assert len(queue.snapshot()["queue"]) == 79
        limited = queue.snapshot(limit=50)["queue"]
        assert len(limited) == 50
        # 保留的是**队首**（马上要播的），不是队尾
        assert limited[0]["title"] == "t1"

    def test_snapshot_default_is_unchanged(self):
        """默认参数下行为必须和以前完全一样，老调用点不受影响。"""
        queue = PlayQueue(idle=False)
        queue.push(QueueItem(keyword="a", title="a", path="/a.mp3"))
        state = queue.snapshot()
        for key in ("queue", "playing", "history", "log", "paused",
                    "volume", "idle", "idle_last", "pending"):
            assert key in state, f"默认快照缺字段：{key}"

    def test_previous_swaps(self):
        queue = PlayQueue(idle=False)
        queue.push(QueueItem(keyword="a", title="a", path="/a.mp3"))
        queue.push(QueueItem(keyword="b", title="b", path="/b.mp3"))
        queue.advance()
        state = queue.previous()
        assert state["playing"]["title"] == "a"
        assert state["queue"][0]["title"] == "b"

    def test_remove(self):
        queue = PlayQueue(idle=False)
        queue.push(QueueItem(keyword="a", title="a", path="/a.mp3"))
        queued = QueueItem(keyword="b", title="b", path="/b.mp3")
        queue.push(queued)
        assert queue.remove(queued.id) is True
        assert queue.remove("nope") is False

    def test_clear(self):
        queue = PlayQueue(idle=False)
        queue.push(QueueItem(keyword="a", title="a", path="/a.mp3"))
        state = queue.clear()
        assert state["playing"] is None and state["queue"] == []

    def test_volume_clamped(self):
        queue = PlayQueue()
        assert queue.set_volume(500)["volume"] == 100
        assert queue.set_volume(-5)["volume"] == 0
        # 非法值不崩，保留上一次的合法音量（不是清零）
        queue.set_volume(42)
        assert queue.set_volume("abc")["volume"] == 42

    def test_idle_random_and_interrupt(self):
        """空闲随机会被真实点歌顶掉。"""
        queue = PlayQueue(idle=True)
        idle_item = QueueItem(keyword="随机", title="随机", path="/r.mp3", idle=True)
        queue.start_now(idle_item)
        assert queue.playing is not None and queue.playing.idle
        queue.push(QueueItem(keyword="晴天", title="晴天", path="/a.mp3"))
        assert queue.playing is not None
        assert queue.playing.title == "晴天"

    def test_log_keeps_bounded(self):
        queue = PlayQueue(log_keep=5)
        for i in range(20):
            queue.note(f"消息 {i}")
        assert len(queue.log_entries()) == 5
        assert queue.log_entries()[-1]["text"] == "消息 19"


# ---------------------------------------------------------------- 模型

class TestModels:
    def test_queue_item_roundtrip(self):
        item = QueueItem(keyword="晴天", title="晴天 - 周杰伦", user="小明",
                         path="/a.mp3", source=TrackSource.LOCAL)
        again = QueueItem.from_dict(item.to_dict())
        assert again.id == item.id
        assert again.source is TrackSource.LOCAL

    def test_unknown_source_falls_back(self):
        item = QueueItem.from_dict({"keyword": "x", "source": "网易云"})
        assert item.source is TrackSource.NONE

    def test_user_truncated(self):
        item = QueueItem.from_track(Track(title="x"), keyword="x", user="超" * 40)
        assert len(item.user) == 16

    def test_started_keeps_idle_flag(self):
        """idle 标记要活到播放期，否则随机歌占住播放位、点歌顶不掉它。"""
        item = QueueItem(keyword="x", title="x", path="/a.mp3", idle=True)
        assert item.with_playback_started().idle is True
        assert item.with_playback_started().started is not None
