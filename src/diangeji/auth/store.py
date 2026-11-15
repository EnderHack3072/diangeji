"""Cookie 持久化。

原版有两处笔误级 bug，这里正面修掉：

1. ``f.write(ck)`` 里 ``ck`` 是 dict —— ``open(path, "w")`` 会**先把文件截断**，
   然后 ``write`` 抛 ``TypeError``。结果：点一次「浏览器登录 QQ」，
   原本能用的 ``qq_cookie.txt`` 变成 0 字节，QQ 音乐从此全部「未找到」。
   这里所有写入都是「先拼成字符串 → 临时文件 → 原子替换」，永远不会有半个文件。

2. 账号切换时旧 cookie 会残留。这里写之前归一化：同名键覆盖，去掉空值。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Mapping

from ..logging_setup import get_logger
from ..paths import data_dir, ensure_dir

log = get_logger(__name__)

BILI_COOKIE = "bili_cookie.txt"
QQ_COOKIE = "qq_cookie.txt"
#: 各平台的有效性判据：有这些键才算「已登录」
REQUIRED_KEYS = {
    BILI_COOKIE: ("SESSDATA",),
    QQ_COOKIE: ("qm_keyst",),
}

_SPLIT_RE = re.compile(r"[;\r\n]+")


def normalize_cookie(value: str | Mapping[str, str]) -> str:
    """把 cookie 归一化成 ``k=v; k=v``。

    接受 ``"a=1; b=2"``、``{"a": "1"}``、多行文本，这三种输入都见过。
    """
    if isinstance(value, Mapping):
        pairs = [(str(k).strip(), str(v).strip()) for k, v in value.items()]
    else:
        pairs = []
        for chunk in _SPLIT_RE.split(str(value or "")):
            if "=" not in chunk:
                continue
            key, _, val = chunk.partition("=")
            pairs.append((key.strip(), val.strip()))
    ordered: dict[str, str] = {}
    for key, val in pairs:
        if key and val:
            ordered[key] = val
    return "; ".join(f"{k}={v}" for k, v in ordered.items())


class CookieStore:
    """按平台读写 cookie 文本。"""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory else data_dir()

    # ---------------- 读 ----------------

    def path(self, filename: str) -> Path:
        return self.directory / filename

    def read(self, filename: str) -> str:
        try:
            return self.path(filename).read_text(encoding="utf-8-sig").strip()
        except OSError:
            return ""

    def has(self, filename: str) -> bool:
        content = self.read(filename)
        required = REQUIRED_KEYS.get(filename, ())
        return bool(content) and all(key in content for key in required)

    def value(self, filename: str, key: str) -> str:
        for part in self.read(filename).split(";"):
            name, _, val = part.strip().partition("=")
            if name == key:
                return val
        return ""

    # ---------------- 写 ----------------

    def write(self, filename: str, cookie: str | Mapping[str, str]) -> bool:
        """原子写入。空内容直接拒绝（绝不把好文件清空）。"""
        text = normalize_cookie(cookie)
        if not text:
            log.warning("拒绝写入空 cookie：%s", filename)
            return False
        target = self.path(filename)
        try:
            ensure_dir(target.parent)
            tmp = target.with_suffix(target.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, target)
        except OSError as exc:
            log.error("写入 %s 失败：%s", filename, exc)
            return False
        log.info("已保存登录态：%s（%d 个键）", filename, text.count("="))
        return True

    def merge(self, filename: str, extra: Mapping[str, str]) -> bool:
        """在已有 cookie 上合并新键（缺什么补什么，不丢旧值）。"""
        existing = self.read(filename)
        merged = dict(self.as_mapping(existing))
        merged.update({k: v for k, v in extra.items() if v})
        return self.write(filename, merged)

    @staticmethod
    def as_mapping(cookie: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in (cookie or "").split(";"):
            key, _, val = part.strip().partition("=")
            if key and val:
                out[key] = val
        return out

    def clear(self, filename: str) -> None:
        try:
            self.path(filename).unlink()
        except OSError:
            pass


def split_pairs(cookie: str) -> list[tuple[str, str]]:
    """调试用：返回 (键, 值) 列表，值不落日志。"""
    items: list[tuple[str, str]] = []
    for part in (cookie or "").split(";"):
        key, _, val = part.strip().partition("=")
        if key:
            items.append((key, val))
    return items
