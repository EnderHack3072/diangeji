"""日志：控制台 + 滚动文件（``点歌姬数据/logs/diangeji.log``）。

Tkinter 界面里的日志框是**另一个**通道（``ui/main_window.py`` 的 ``log()`` 直接往 Text 里写），
这里给的是排障用的落盘日志。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .paths import data_dir, ensure_dir

LOGGER_NAME = "diangeji"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str = "") -> logging.Logger:
    """取模块 logger，统一挂在 ``diangeji`` 这棵树上。"""
    if not name:
        return logging.getLogger(LOGGER_NAME)
    if name.startswith(LOGGER_NAME):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def _safe_stream(stream):
    """把控制台输出改成 UTF-8。

    Windows 控制台默认是 GBK 代码页，中文日志直接 ``SystemExit`` 级乱码；
    更糟的是某些代码页下编码失败会抛 ``UnicodeEncodeError`` 把日志打挂。
    这里尽量换成 UTF-8，换不了就保留原流 —— 文件日志始终是 UTF-8 不受影响。
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
            return stream
        except (OSError, ValueError):  # pragma: no cover - 特殊流
            pass
    return stream


def setup(level: int = logging.INFO, log_file: Path | None = None,
          console: bool = True) -> logging.Logger:
    """初始化根 logger，重复调用不会叠加 handler。"""
    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    if console:
        # 打包成 --windowed 后 stderr 可能是 None，写日志会炸，务必判空
        stream = sys.stderr
        if stream is not None:
            try:
                out = logging.StreamHandler(_safe_stream(stream))
                out.setFormatter(formatter)
                root.addHandler(out)
            except (OSError, ValueError):
                pass

    target = log_file or (data_dir() / "logs" / "diangeji.log")
    try:
        ensure_dir(target.parent)
        file_handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        # 目录不可写就退化成纯控制台，不影响主功能
        pass

    root.propagate = False
    return root
