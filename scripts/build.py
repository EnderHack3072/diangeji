"""打包脚本：用 PyInstaller 打成单文件 exe。

与旧版 ``main.py`` 直接打包的差别：模板和图片现在都在包内，
必须显式作为 data 带进去，否则打出来的 exe 打开是白屏。

用法::

    python scripts/build.py            # 生成 dist/点歌姬.exe
    python scripts/build.py --dir      # 打成一个目录（启动更快）
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PACKAGE = SRC / "diangeji"
DIST = ROOT / "dist"
BUILD = ROOT / "build"

APP_NAME = "点歌姬"
ENTRY = ROOT / "run.py"


def collect_datas() -> list[tuple[str, str]]:
    """要打进 exe 的资源：模板 + 图片。路径按包内相对位置摆放。"""
    datas: list[tuple[str, str]] = []
    templates = PACKAGE / "web" / "templates"
    images = PACKAGE / "web" / "static" / "img"
    if templates.is_dir():
        datas.append((str(templates), "diangeji/web/templates"))
    if images.is_dir():
        datas.append((str(images), "diangeji/web/static/img"))
    return datas


def check_prerequisites() -> bool:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller：python -m pip install pyinstaller", file=sys.stderr)
        return False
    if not ENTRY.is_file():
        print(f"找不到入口脚本：{ENTRY}", file=sys.stderr)
        return False
    return True


def build(one_file: bool = True, clean: bool = True) -> int:
    if not check_prerequisites():
        return 1
    if clean:
        for directory in (DIST, BUILD):
            shutil.rmtree(directory, ignore_errors=True)

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", APP_NAME,
        "--windowed",
        "--paths", str(SRC),
    ]
    if one_file:
        command.append("--onefile")

    for source, destination in collect_datas():
        command += ["--add-data", f"{source}{';' if sys.platform == 'win32' else ':'}{destination}"]

    icon = PACKAGE / "web" / "static" / "img" / "icon.ico"
    if icon.is_file():
        command += ["--icon", str(icon)]

    # 可选依赖：装了就打进去，没装也不影响
    for module in ("qrcode", "png", "brotli"):
        try:
            __import__(module)
            command += ["--hidden-import", module]
        except ImportError:
            pass

    command.append(str(ENTRY))
    print("执行：", " ".join(command))
    return subprocess.call(command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包点歌姬")
    parser.add_argument("--dir", action="store_true", help="打成目录而不是单文件")
    parser.add_argument("--no-clean", action="store_true", help="保留上次的 build 缓存")
    args = parser.parse_args(argv)
    return build(one_file=not args.dir, clean=not args.no_clean)


if __name__ == "__main__":
    sys.exit(main())
