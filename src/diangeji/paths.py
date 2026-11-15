"""路径解析：源码运行 / PyInstaller 打包 / 已安装三种形态都要能跑。

约定：

* **资源**（模板、图片）随包分发，冻结后位于 ``_MEIPASS/diangeji``。
* **数据**（cookie、配置、日志、本地曲库）永远在可写目录，绝不进包。
"""

from __future__ import annotations

import os
import sys
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
    """程序所在目录。

    * 冻结后：exe 所在目录（数据跟着 exe 走，符合原有的绿色软件习惯）。
    * 源码运行且是 ``src/`` 布局：项目根目录。
    * 其他（已安装到 site-packages）：当前工作目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    root = _DEFAULT_BASE
    if root.parent.name == "src":
        return root.parent.parent
    return Path.cwd()


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


def icon_path() -> Path:
    return image_dir() / "icon.ico"
