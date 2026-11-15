"""模板渲染：把状态注入 HTML 时的转义。

原版只做 ``</`` → ``<\\/``，这对「注入进 ``<script>`` 里的 JSON」够用，
但换到别的上下文就不够。这里集中成一个函数，并且**只用于 script 上下文**，
所有插入 DOM 的文本仍由前端 ``esc()`` 负责。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

STATE_PLACEHOLDER = "__STATE__"
LIBCOUNT_PLACEHOLDER = "__LIBCOUNT__"
VERSION_PLACEHOLDER = "__VERSION__"


def render_state_script(state: Any) -> str:
    """序列化成可安全内嵌进 ``<script>`` 的 JSON。

    依次处理：

    1. ``</`` → ``<\\/``  防止提前闭合 ``</script>``
    2. U+2028 / U+2029 转义（老 JS 引擎会当换行，直接语法错误）
    3. ``<!--`` → ``<\\!--``  防止 HTML 注释开始
    """
    text = json.dumps(state, ensure_ascii=False)
    text = text.replace("</", "<\\/")
    text = text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return text.replace("<!--", "<\\!--")


def _read(template_dir: Path, name: str) -> str:
    return (Path(template_dir) / name).read_text(encoding="utf-8")


def render_console(template_dir: Path, state: Any, lib_count: int, version: str) -> str:
    """主播台：注入初始状态 + 曲目数 + 版本号。"""
    html = _read(template_dir, "index.html")
    return (html.replace(STATE_PLACEHOLDER, render_state_script(state))
                .replace(LIBCOUNT_PLACEHOLDER, str(int(lib_count)))
                .replace(VERSION_PLACEHOLDER, version))


def render_overlay(template_dir: Path, version: str = "") -> str:
    """OBS 覆盖层：不带状态（前端自己轮询）。"""
    html = _read(template_dir, "overlay.html")
    return html.replace(VERSION_PLACEHOLDER, version)
