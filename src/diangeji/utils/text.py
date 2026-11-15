"""字符串小工具。纯函数、无副作用、无依赖。"""

from __future__ import annotations

import re
import unicodedata

#: 点歌触发词（与 constants.ORDER_PREFIXES 保持一致；此处内置一份避免循环导入）
_PREFIX_RE = re.compile(r"^(?:点歌|点播|来一首|想听)\s*[:：,，、]?\s*(.+)$")

#: Windows 文件名非法字符
_BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Windows 保留设备名：叫 CON 的文件在资源管理器里根本建不出来
_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


def strip_order_prefix(text: str) -> str:
    """去掉「点歌 / 点播 / 来一首 / 想听」前缀并清理分隔符；不是点歌指令就原样返回。"""
    raw = (text or "").strip()
    match = _PREFIX_RE.match(raw)
    if not match:
        return raw
    return match.group(1).strip(" :：,，、\t")


def ellipsize(text: str, limit: int = 40, suffix: str = "…") -> str:
    """超长截断。按**字符**计，不做像素测量。"""
    value = text or ""
    if limit <= 0 or len(value) <= limit:
        return value
    return value[:limit] + suffix


def clean_filename(name: str, fallback: str = "untitled") -> str:
    """把任意字符串变成可用的文件名（保留中文）。

    Windows 上 ``CON`` / ``PRN`` / ``COM1`` 这类设备名不能当文件名用，
    即使加了扩展名也一样 —— 所以整体加上前缀避开。
    """
    value = unicodedata.normalize("NFKC", name or "").strip()
    value = _BAD_FILENAME.sub("_", value).strip(" .")
    if not value:
        return fallback
    stem = value.split(".")[0].upper()
    if stem in _RESERVED:
        value = "_" + value
    return value


def escape_js_string(text: str) -> str:
    """把 JSON 内联进 ``<script>`` 时兜底：转义 ``</`` 防止提前闭合标签。"""
    return (text or "").replace("</", "<\\/")


def humanize_seconds(seconds: float) -> str:
    """``3725`` → ``1:02:05``。"""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
