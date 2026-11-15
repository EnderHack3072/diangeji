"""本地曲库：扫描目录、按关键词匹配。

相对原版修了三处：

1. 真递归扫描（README 早就这么写了，代码原来只扫一层），带深度上限防止软链接成环。
2. 匹配从「第一个 substring 命中」改成 **完全相等 → 前缀 → 子串** 的分级匹配，
   点「晴天」不会因为曲库里有「晴天娃娃」排前面就串台。
3. 文件列表按「路径 → 标题」缓存，``/media/`` 不再靠 ``endswith`` 猜路径。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Iterable, Sequence

from ..constants import AUDIO_EXTENSIONS, MIN_AUDIO_BYTES
from ..logging_setup import get_logger

log = get_logger(__name__)

MAX_SCAN_DEPTH = 6


def title_of(path: str | Path) -> str:
    """``D:/music/晴天_live.mp3`` → ``晴天 live``。"""
    base = os.path.splitext(os.path.basename(str(path)))[0]
    return base.replace("_", " ").strip()


class LocalLibrary:
    """本地曲库。线程安全（``scan`` 在启动线程里跑，``find`` 在 HTTP 线程里调）。"""

    def __init__(self) -> None:
        self._tracks: list[dict[str, object]] = []
        self._by_path: dict[str, dict[str, object]] = {}
        self._lock = threading.RLock()

    # ---- 查询 ----

    def __len__(self) -> int:
        with self._lock:
            return len(self._tracks)

    def __iter__(self):
        with self._lock:
            return iter(list(self._tracks))

    @property
    def count(self) -> int:
        return len(self)

    def entries(self) -> list[dict[str, object]]:
        """全部曲目（副本），给 ``/api/library`` 和搜索接口用。"""
        with self._lock:
            return [dict(t) for t in self._tracks]

    def sample(self, limit: int = 20) -> list[dict[str, object]]:
        with self._lock:
            return [dict(t) for t in self._tracks[:limit]]

    def find(self, keyword: str) -> dict[str, object] | None:
        """按关键词找一首本地曲目，找不到返回 ``None``。

        分级：完全相等 > 前缀 > 子串；同级别取标题最短的（越短越像原曲）。
        """
        k = (keyword or "").strip().lower()
        if not k:
            return None
        with self._lock:
            candidates = list(self._tracks)
        for matcher in (
            lambda title: title == k,
            lambda title: title.startswith(k),
            lambda title: k in title,
        ):
            hits = [t for t in candidates if matcher(str(t["title"]).lower())]
            if hits:
                best = min(hits, key=lambda t: len(str(t["title"])))
                return dict(best)
        return None

    def search(self, keyword: str = "", limit: int = 20) -> list[dict[str, object]]:
        k = (keyword or "").strip().lower()
        with self._lock:
            hits = [dict(t) for t in self._tracks if not k or k in str(t["title"]).lower()]
        return hits[: max(0, limit)]

    def path_for(self, rel: str) -> str | None:
        """``/media/<rel>`` 的解析：先精确匹配绝对路径，再按路径后缀精确匹配。

        原实现用 ``endswith``，``rel`` 为空时恒真（会把曲库第一首吐给 ``GET /media/``），
        且 ``x.mp3`` 能命中 ``name.mp3``。这里要求 ``rel`` 非空且按分隔符对齐。
        """
        key = (rel or "").strip()
        if not key:
            return None
        normalized = key.replace("/", os.sep)
        with self._lock:
            hit = self._by_path.get(normalized) or self._by_path.get(os.path.abspath(normalized))
            if hit:
                return str(hit["path"])
            suffix = os.sep + normalized.lstrip(os.sep)
            for path in self._by_path:
                if path.endswith(suffix):
                    return path
        return None

    # ---- 扫描 ----

    def scan(self, dirs: Sequence[str] | Iterable[str] = (), files: Iterable[str] = (),
             recursive: bool = True) -> int:
        """重扫曲库，返回曲目数。单个目录出错不影响其他目录。"""
        found: list[dict[str, object]] = []
        seen: set[str] = set()

        for directory in dirs or ():
            for path in self._walk(directory, recursive=recursive):
                if path in seen:
                    continue
                seen.add(path)
                title = title_of(path)
                found.append({"title": title, "path": path, "mid": None, "source": "local"})

        for item in files or ():
            try:
                path = os.path.abspath(str(item))
                if not os.path.isfile(path) or path in seen:
                    continue
                seen.add(path)
                found.append({"title": title_of(path), "path": path, "mid": None,
                              "source": "local"})
            except OSError:
                continue

        found.sort(key=lambda t: str(t["title"]).lower())
        with self._lock:
            self._tracks = found
            self._by_path = {str(t["path"]): t for t in found}
        log.info("本地曲库扫描完成：%d 首", len(found))
        return len(found)

    @staticmethod
    def _walk(directory: str, recursive: bool = True) -> Iterable[str]:
        """遍历目录产出可用音频的绝对路径。"""
        if not directory:
            return
        root = os.path.abspath(str(directory))
        if not os.path.isdir(root):
            return
        base_depth = root.rstrip(os.sep).count(os.sep)
        for current, subdirs, names in os.walk(root, followlinks=False):
            if not recursive:
                subdirs[:] = []
            elif current.count(os.sep) - base_depth >= MAX_SCAN_DEPTH:
                subdirs[:] = []
            for name in names:
                if not name.lower().endswith(AUDIO_EXTENSIONS):
                    continue
                path = os.path.join(current, name)
                try:
                    if os.path.getsize(path) < MIN_AUDIO_BYTES:
                        continue
                except OSError:
                    continue
                yield path

    # ---- 兼容 ----

    def as_legacy_list(self) -> list[dict[str, object]]:
        """旧代码里 ``LIB`` 那种 ``[{"title","path","mid","source"}]`` 结构。"""
        return self.entries()
