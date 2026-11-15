"""配置读写：``点歌姬数据/config.json``。

只放**用户可调**的设置。运行时状态（队列、播放中）不放这里，
登录态单独存 cookie 文件，避免一把 JSON 混装敏感数据。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .paths import atomic_write, data_dir

CONFIG_FILENAME = "config.json"


@dataclass
class Config:
    """用户配置。字段名即 JSON 键名。"""

    room_id: str = ""
    volume: int = 20
    idle_enabled: bool = True
    local_dirs: list[str] = field(default_factory=list)
    auto_start: bool = False

    # ---- 校验 ----

    def normalized(self) -> "Config":
        """把越界值拉回合法范围，返回自身（链式调用）。"""
        self.room_id = str(self.room_id or "").strip()
        try:
            self.volume = max(0, min(100, int(self.volume)))
        except (TypeError, ValueError):
            self.volume = 20
        self.idle_enabled = bool(self.idle_enabled)
        self.auto_start = bool(self.auto_start)
        if not isinstance(self.local_dirs, list):
            self.local_dirs = []
        self.local_dirs = [str(p) for p in self.local_dirs if str(p).strip()]
        return self

    # ---- 磁盘 ----

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """读取配置；文件缺失或损坏时返回默认值，绝不抛异常。"""
        target = path or (data_dir() / CONFIG_FILENAME)
        try:
            with open(target, "r", encoding="utf-8-sig") as fh:
                raw: Any = json.load(fh)
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known}).normalized()

    def save(self, path: Path | None = None) -> Path:
        """原子写入配置（先写临时文件再替换，断电不留半个 JSON）。"""
        target = path or (data_dir() / CONFIG_FILENAME)
        atomic_write(target, json.dumps(asdict(self), ensure_ascii=False, indent=2))
        return target
