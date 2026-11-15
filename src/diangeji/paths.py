"""路径解析：源码运行 / PyInstaller 打包 / 已安装三种形态都要能跑。

约定：

* **资源**（模板、图片）随包分发，冻结后位于 ``_MEIPASS/diangeji``。
* **数据**（cookie、配置、日志、本地曲库）永远在可写目录，绝不进包。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from pathlib import Path

PACKAGE_NAME = "diangeji"
DATA_DIR_NAME = "点歌姬数据"
_DEFAULT_BASE = Path(__file__).resolve().parent


def is_frozen() -> bool:
    """是否在 PyInstaller 等打包环境里运行。"""
    return bool(getattr(sys, "frozen", False)) or bool(getattr(sys, "_MEIPASS", None))


def package_root() -> Path:
    """包目录：含 ``web/`` ``ui/`` 等子包的那一级。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / PACKAGE_NAME
        if candidate.is_dir():
            return candidate
    return _DEFAULT_BASE


def base_dir() -> Path:
    """数据目录的**父目录** —— :func:`data_dir` 会在它下面再建一层。

    * 冻结后：exe 所在目录（数据跟着 exe 走，符合绿色软件习惯）。
    * 源码运行且是 ``src/`` 布局：项目根目录。
    * 已安装到 site-packages：用户级目录，不跟 cwd 走。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    root = _DEFAULT_BASE
    if root.parent.name == "src":
        return root.parent.parent
    return user_base_dir()


def user_base_dir() -> Path:
    """按平台给一个稳定的用户级目录。

    pip 装的包没有「程序目录」可言 —— 如果用 ``Path.cwd()``，从哪个目录启动
    就有一份独立数据，换个目录启动等于配置和登录全丢。这里固定到用户目录：

    * Windows：``%LOCALAPPDATA%``
    * macOS：``~/Library/Application Support``
    * 其他：``$XDG_DATA_HOME`` 或 ``~/.local/share``
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base) if base else Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) if xdg else Path.home() / ".local" / "share"


def data_dir() -> Path:
    """运行数据目录，可用 ``DIANGE_DATA`` 环境变量覆盖。"""
    override = os.environ.get("DIANGE_DATA")
    if override:
        return Path(override).expanduser().resolve()
    return base_dir() / DATA_DIR_NAME


def ensure_dir(path: Path) -> Path:
    """建目录并返回它（``parents=True, exist_ok=True``）。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write(target: Path, text: str, *, encoding: str = "utf-8") -> None:
    """写临时文件再 ``os.replace`` —— 断电不会留半个文件。

    临时文件名带 pid + 线程 id + 随机后缀：多个线程同时写同一个目标时，
    共用一个 ``.tmp`` 会互相踩（Windows 上表现为 Permission denied /
    WinError 32），抢到的那个 ``os.replace`` 还可能换上半截内容。

    失败时清掉临时文件，数据目录里不留垃圾。

    Windows 上 ``os.replace`` 覆盖一个正被别的线程/进程打开的目标会抛
    ``PermissionError``（POSIX 不会），所以替换这一步带退避重试。
    """
    ensure_dir(target.parent)
    tmp = target.with_name(
        f"{target.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding=encoding) as fh:
            fh.write(text)
        _replace_with_retry(tmp, target)
    except OSError:
        discard(tmp)
        raise


def _replace_with_retry(source: Path, target: Path, attempts: int = 20) -> None:
    """``os.replace`` 带退避重试（Windows 并发覆盖会短暂占用目标）。"""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.01 * (attempt + 1))


def discard(path: Path) -> None:
    """尽力删除一个文件，删不掉也不吭声（清理路径专用）。"""
    try:
        path.unlink()
    except OSError:
        pass


# ---- 包内资源位置（只读） ----

def resource_dir(*parts: str) -> Path:
    """包内只读资源目录，例如 ``resource_dir("web", "templates")``。"""
    return package_root().joinpath(*parts)


def template_dir() -> Path:
    return resource_dir("web", "templates")


def static_dir() -> Path:
    return resource_dir("web", "static")


def image_dir() -> Path:
    return static_dir() / "img"


def button_image_dir() -> Path:
    return image_dir() / "buttons"


