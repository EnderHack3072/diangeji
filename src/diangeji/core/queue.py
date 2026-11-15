"""播放队列状态机。

原 ``server.py`` 把队列、播放中、历史、日志、音量、空闲标记全塞在一个
``STATE`` 字典里，谁都能改，且网络请求（``freshen``）是在锁里跑的。
这里收成一个对象，明确三条不变量：

1. **所有写操作走 lock**，读操作 ``snapshot()`` 拿深拷贝，外面改不到内部状态。
2. **锁内不做 I/O**。需要联网刷新直链时，用 ``advance(refresh=...)`` /
   ``begin()`` 把回调交给调用方在锁外执行，再回到锁内提交。
3. ``playing is None`` 且 ``idle`` 为真时**才**触发空闲随机，由调用方决定是否播。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from ..constants import HISTORY_KEEP, LOG_KEEP
from ..logging_setup import get_logger
from ..utils.timeutil import now_ts
from .models import QueueItem

log = get_logger(__name__)

RefreshFn = Callable[[QueueItem], QueueItem]


class PlayQueue:
    """点歌队列 + 播放状态。线程安全，可单测（无网络、无 UI）。"""

    def __init__(self, volume: int = 20, idle: bool = True,
                 history_keep: int = HISTORY_KEEP, log_keep: int = LOG_KEEP) -> None:
        self._lock = threading.RLock()
        self._queue: list[QueueItem] = []
        self._playing: QueueItem | None = None
        self._history: list[QueueItem] = []
        self._log: list[dict[str, Any]] = []
        self.paused: bool = False
        self.volume: int = max(0, min(100, int(volume)))
        self.idle: bool = bool(idle)
        self.idle_last: str = ""
        self.last_seen: float = 0.0
        self.pending: int = 0
        self._history_keep = history_keep
        self._log_keep = log_keep

    # ---------------- 读取 ----------------

    @property
    def playing(self) -> QueueItem | None:
        with self._lock:
            return self._playing

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._queue)

    def log_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._log]

    def snapshot(self, *, include_log: bool = True,
                 limit: int | None = None) -> dict[str, Any]:
        """给前端 / 界面的只读快照（JSON 可序列化）。

        所有这些 ``to_dict()`` 都在 ``_lock`` **里面**做，而 ``push()`` /
        ``advance()`` 抢的是同一把锁 —— 所以前端轮询越勤、队列越长，
        点歌入队就被挡得越久。热门直播间队列几百条时这不是空谈。

        参数就是给轮询路径用的：

        * ``include_log=False`` —— 日志只有界面那个日志框要，``/api/state``
          每 2 秒拉一次没必要带上最近 40 条。
        * ``limit=N`` —— 只回队列前 N 条。前端本来就只渲染前几十条。

        默认值保持「全都要」，所以老的调用点行为不变。
        """
        with self._lock:
            queue = self._queue if limit is None else self._queue[:max(0, limit)]
            payload: dict[str, Any] = {
                "queue": [item.to_dict() for item in queue],
                "playing": self._playing.to_dict() if self._playing else None,
                "history": [item.to_dict() for item in self._history],
                "paused": self.paused,
                "volume": self.volume,
                "idle": self.idle,
                "idle_last": self.idle_last,
                "pending": self.pending,
            }
            if include_log:
                payload["log"] = [dict(e) for e in self._log]
            return payload

    def needs_idle(self) -> bool:
        """是否需要补一首空闲随机。"""
        with self._lock:
            return self._playing is None and bool(self.idle)

    # ---------------- 日志 ----------------

    def note(self, text: str) -> None:
        """写一条界面可见的指令日志。"""
        with self._lock:
            self._log.append({"t": now_ts(), "text": str(text)})
            if len(self._log) > self._log_keep:
                del self._log[: len(self._log) - self._log_keep]

    # ---------------- 网络占位 ----------------

    @contextmanager
    def resolving(self) -> Iterator[None]:
        """标记「有一个解析请求在外面跑」，让空闲随机避让。"""
        with self._lock:
            self.pending += 1
        try:
            yield
        finally:
            with self._lock:
                self.pending = max(0, self.pending - 1)

    def is_resolving(self) -> bool:
        with self._lock:
            return self.pending > 0

    # ---------------- 入队 / 出队 ----------------

    def push(self, item: QueueItem) -> bool:
        """入队。返回 ``True`` 表示它直接顶上播放位。"""
        with self._lock:
            if self._playing is not None and self._playing.idle:
                self._playing = None
                self.note("收到点歌，切掉随机播放")
            if item.playable and self._playing is None:
                self._playing = item.with_playback_started()
                self.note(f"{item.user} 点歌 {item.keyword} → 开播")
                return True
            self._queue.append(item)
            self.note(f"{item.user} 点歌 {item.keyword} → "
                      + ("入队" if item.playable else "未找到"))
            return False

    def _pop_playable_locked(self) -> QueueItem | None:
        """队首起取第一个能播的；缺音源的顺手丢掉并记日志。调用方需持锁。"""
        while self._queue:
            candidate = self._queue.pop(0)
            if candidate.playable:
                return candidate
            self.note("跳过（缺音源）：" + candidate.title)
        return None

    def advance(self, refresh: RefreshFn | None = None) -> dict[str, Any]:
        """下一首：当前进历史，从队列取下一个。``refresh`` 在锁外执行。"""
        return self._advance(refresh, None)[1]

    def advance_if_playing(self, item_id: str,
                           refresh: RefreshFn | None = None) -> tuple[bool, dict[str, Any]]:
        """只有当前播放项 id 与 ``item_id`` 相符才切歌（CAS）。

        返回 ``(是否切了, 快照)``。「比对 id」和「占住播放位」在同一把锁里完成 ——
        否则两个页面（主播台静音 + OBS 层）同时上报 ended 时，可能各自通过
        校验再各切一次，就是最初那个「一首歌跳两首」。
        """
        return self._advance(refresh, item_id)

    def _advance(self, refresh: RefreshFn | None,
                 expected_id: str | None) -> tuple[bool, dict[str, Any]]:
        with self._lock:
            current = self._playing
            if expected_id is not None:
                if current is None or current.id != expected_id:
                    log.debug("忽略过期的播放结束事件：%s（当前 %s）",
                              expected_id, current.id if current else None)
                    return False, self.snapshot()
            if current is not None:
                self._push_history_locked(current)
            self._playing = None
            candidate = self._pop_playable_locked()

        if candidate is not None and refresh is not None:
            try:
                candidate = refresh(candidate)  # 网络：QQ 直链刷新
            except Exception as exc:  # pragma: no cover - 刷新失败就按原样播
                log.warning("刷新直链失败：%s", exc)

        with self._lock:
            if self._playing is None:
                if candidate is not None:
                    self._playing = candidate.with_playback_started()
                    self.note("下一首 → " + candidate.title)
                else:
                    self.note("队列空了")
            elif candidate is not None:
                # 锁外那段时间有人点歌抢了播放位，把它塞回队首
                self._queue.insert(0, candidate)
            return True, self.snapshot()

    def previous(self, refresh: RefreshFn | None = None) -> dict[str, Any]:
        """上一首：历史弹回播放位，当前进队首。"""
        with self._lock:
            if not self._history:
                self.note("没有上一首")
                return self.snapshot()
            target = self._history.pop()
            if self._playing is not None:
                self._queue.insert(0, self._playing)
            self._playing = None

        if refresh is not None:
            try:
                target = refresh(target)
            except Exception as exc:  # pragma: no cover
                log.warning("刷新直链失败：%s", exc)

        with self._lock:
            # 锁外刷直链这段时间，可能有人点歌把播放位顶了（push → 开播）。
            # 无条件赋值会把那一首吞掉，所以和 advance() 保持同样的保护。
            if self._playing is None:
                self._playing = target.with_playback_started()
                self.note("上一首 → " + target.title)
            else:
                self._history.append(target)
                self.note("上一首被新点歌抢位，退回历史")
            return self.snapshot()

    def start_now(self, item: QueueItem) -> None:
        """直接把一项顶上播放位（空闲随机用）。"""
        with self._lock:
            self._playing = item.with_playback_started()
            if item.idle:
                self.idle_last = item.keyword
            self.note(("空闲随机 → " if item.idle else "开播 → ") + item.title)

    def _push_history_locked(self, item: QueueItem) -> None:
        self._history.append(item)
        if len(self._history) > self._history_keep:
            del self._history[: len(self._history) - self._history_keep]

    def remove(self, item_id: str) -> bool:
        with self._lock:
            before = len(self._queue)
            self._queue = [x for x in self._queue if x.id != item_id]
            return len(self._queue) != before

    def clear(self) -> dict[str, Any]:
        with self._lock:
            self._queue.clear()
            self._playing = None
            self._history.clear()
            self.paused = False
            self.note("清空队列与播放")
            return self.snapshot()

    # ---------------- 播放控制 ----------------

    def set_paused(self, value: bool | None = None) -> dict[str, Any]:
        with self._lock:
            self.paused = (not self.paused) if value is None else bool(value)
            self.note("暂停播放" if self.paused else "继续播放")
            return self.snapshot()

    def set_volume(self, value: int) -> dict[str, Any]:
        with self._lock:
            try:
                self.volume = max(0, min(100, int(value)))
            except (TypeError, ValueError):
                pass
            return self.snapshot()

    def set_idle(self, value: bool | None = None) -> dict[str, Any]:
        with self._lock:
            self.idle = (not self.idle) if value is None else bool(value)
            self.note("空闲随机开启" if self.idle else "空闲随机关闭")
            return self.snapshot()

    def touch(self) -> None:
        """页面在轮询，说明有人连着（关程序前多等一会儿用）。"""
        with self._lock:
            self.last_seen = time.time()

    def last_seen_within(self, seconds: float) -> bool:
        with self._lock:
            return (time.time() - self.last_seen) < seconds
