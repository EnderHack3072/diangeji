"""曲库服务：把「用户配置的目录」和「默认目录」拼成扫描列表。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Sequence

from ..core.library import LocalLibrary
from ..logging_setup import get_logger

log = get_logger(__name__)


def default_music_dirs(data_dir: Path) -> list[str]:
    """默认扫描位置：``<数据目录>/music`` 和 ``~/Music``。"""
    candidate = [str(Path(data_dir) / "music"), os.path.expanduser("~/Music")]
    return [path for path in candidate if path]


class LibraryService:
    """扫描本地曲库。界面和 HTTP 都用它，保证只有一份曲库实例。"""

    def __init__(self, library: LocalLibrary, data_dir: Path) -> None:
        self.library = library
        self.data_dir = Path(data_dir)

    def scan(self, extra_dirs: Sequence[str] = ()) -> int:
        """扫描并返回曲目数。目录去重，坏目录跳过。"""
        dirs = _dedupe([*default_music_dirs(self.data_dir), *extra_dirs])
        try:
            return self.library.scan(dirs)
        except Exception as exc:  # pragma: no cover - 扫描失败不该拖垮启动
            log.error("曲库扫描失败：%s", exc)
            return 0


def _dedupe(paths: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in paths:
        if not raw:
            continue
        key = os.path.normcase(os.path.abspath(str(raw)))
        if key in seen:
            continue
        seen.add(key)
        out.append(str(raw))
    return out
