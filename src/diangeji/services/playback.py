"""播放编排：点歌、切歌、空闲随机的唯一入口。

原版这些逻辑散在 ``server.py`` 的模块级函数里，且 ``freshen()`` 在锁内联网。
这里所有会阻塞的操作（解析、刷直链）都在**锁外**执行，``PlayQueue`` 只负责状态提交。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Callable, Iterable

from ..core.models import QueueItem, Track, TrackSource
from ..core.queue import PlayQueue
from ..logging_setup import get_logger
from ..sources.registry import SourceRegistry

log = get_logger(__name__)

#: 空闲随机失败后重试的次数
IDLE_RETRIES = 8


class PlaybackService:
    """点歌 / 切歌 / 空闲随机。线程安全，可被 HTTP 线程和界面线程同时调用。"""

    def __init__(self, queue: PlayQueue, registry: SourceRegistry,
                 idle_pool: Iterable[str] = (), favorites: Callable[[], list[dict]] | None = None,
                 rng: random.Random | None = None) -> None:
        self.queue = queue
        self.registry = registry
        self._idle_pool = [x for x in idle_pool if str(x).strip()]
        self._favorites = favorites or (lambda: [])
        self._rng = rng or random.Random()
        self._idle_lock = threading.Lock()

    # ---------------- 点歌 ----------------

    def request(self, user: str, keyword: str) -> QueueItem:
        """解析并入队。解析在锁外，入队时再提交。

        * 本地曲库优先，找不到走 QQ 音乐。
        * 解析失败也入队（``path=None``），前端标「缺音源」，不会静默吞掉观众的请求。
        """
        key = (keyword or "").strip()
        if not key:
            raise ValueError("空的点歌关键词")

        with self.queue.resolving():
            result = self.registry.resolve(key)

        track = result.track or Track(title=key, source=TrackSource.NONE)
        item = QueueItem.from_track(track, keyword=key, user=user)
        self.queue.push(item)
        return item

    # ---------------- 切歌 ----------------

    def next(self) -> dict:
        snapshot = self.queue.advance(refresh=self._refresh_item)
        self._maybe_idle()
        return snapshot

    def previous(self) -> dict:
        snapshot = self.queue.previous(refresh=self._refresh_item)
        self._maybe_idle()
        return snapshot

    def skip_if_finished(self, item_id: str) -> bool:
        """播放器报告「唱完了」。

        只有 **当前播放的那一条** 且 ID 对得上才切歌 —— 这是修掉
        「开两个页面（主播台静音 + OBS 层）导致一首歌跳两首」的关键：
        后到的那个 ended 事件会因为 ID 已变而被丢弃。
        """
        current = self.queue.playing
        if current is None or current.id != item_id:
            log.debug("忽略过期的播放结束事件：%s", item_id)
            return False
        self.next()
        return True

    def _refresh_item(self, item: QueueItem) -> QueueItem:
        """锁外刷新直链（QQ 音乐地址二十分钟过期）。"""
        track = Track(title=item.title, path=item.path, mid=item.mid,
                      source=item.source)
        refreshed = self.registry.refresh(track)
        if refreshed is None or refreshed.path == item.path:
            return item
        return item.with_path(refreshed.path)

    # ---------------- 空闲随机 ----------------

    def set_idle_pool(self, pool: Iterable[str]) -> None:
        """替换内置空闲歌单（``点歌姬数据/idle_pool.txt`` 覆盖用）。"""
        self._idle_pool = [str(x).strip() for x in pool if str(x).strip()]

    def set_idle(self, value: bool | None = None) -> dict:
        snapshot = self.queue.set_idle(value)
        self._maybe_idle()
        return snapshot

    def _pool(self) -> list[tuple[str, str | None]]:
        """候选池：优先 QQ 音乐「我喜欢」，退回内置歌单。"""
        picks: list[tuple[str, str | None]] = []
        try:
            for fav in self._favorites() or []:
                name = f'{fav.get("title", "")} - {fav.get("singer") or ""}'.strip(" -")
                if name:
                    picks.append((name, fav.get("mid")))
        except Exception as exc:  # pragma: no cover - 收藏夹接口挂了不影响内置表
            log.debug("拉收藏失败：%s", exc)
        if not picks:
            picks = [(name, None) for name in self._idle_pool]
        return picks

    def _maybe_idle(self) -> None:
        """队列空了且开了空闲随机 → 补一首。"""
        if not self.queue.needs_idle() or self.queue.is_resolving():
            return
        if not self._idle_lock.acquire(blocking=False):
            return
        try:
            self._idle_once()
        finally:
            self._idle_lock.release()

    def _idle_once(self) -> None:
        picks = self._pool()
        if not picks:
            return
        last = self.queue.idle_last
        for _ in range(IDLE_RETRIES):
            name, mid = self._rng.choice(picks)
            if name == last and len(picks) > 1:
                continue
            item = self._build_idle_item(name, mid)
            if item is not None:
                self.queue.start_now(item)
                return

    def _build_idle_item(self, name: str, mid: str | None) -> QueueItem | None:
        """把候选歌变成可播条目；拿不到直链就放弃这一首。"""
        if mid:
            track = self.registry.refresh(Track(title=name, mid=mid, source=TrackSource.QQ))
            if track is None or not track.playable:
                return None
            return QueueItem(title=track.title or name, keyword=name, user="随机",
                             path=track.path, source=TrackSource.QQ, mid=track.mid,
                             idle=True, started=int(time.time()))
        result = self.registry.resolve(name)
        if result.track is None or not result.track.playable:
            return None
        item = QueueItem.from_track(result.track, keyword=name, user="随机")
        return item.copy_as_detached(user="随机", idle=True)

    # ---------------- 直通 ----------------

    def set_paused(self, value: bool | None = None) -> dict:
        return self.queue.set_paused(value)

    def set_volume(self, value: int) -> dict:
        return self.queue.set_volume(value)

    def remove(self, item_id: str) -> dict:
        self.queue.remove(item_id)
        return self.queue.snapshot()

    def clear(self) -> dict:
        return self.queue.clear()

    def snapshot(self) -> dict:
        return self.queue.snapshot()
