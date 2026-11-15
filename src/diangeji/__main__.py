"""命令行入口：``python -m diangeji`` / ``diangeji``。

支持两种模式：

* 默认：Tkinter 界面（打包成 exe 的样子）
* ``--headless``：只跑 HTTP 服务，不开界面（调试 / 服务器上用）
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .app import build_application
from .constants import DEFAULT_PORT
from .logging_setup import get_logger, setup as setup_logging

log = get_logger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="diangeji",
        description="点歌姬 · B 站直播间弹幕点歌机",
    )
    parser.add_argument("--version", action="version", version=f"点歌姬 v{__version__}")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="运行数据目录（默认 <程序目录>/点歌姬数据）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="本地服务端口")
    parser.add_argument("--room", default="", help="直播间房间号（纯数字）")
    parser.add_argument("--headless", action="store_true", help="不开界面，只跑 HTTP 服务")
    parser.add_argument("--allow-headless-api", action="store_true",
                        help="允许无 Origin/Referer 的客户端调 /api/*（本机脚本用）")
    parser.add_argument("--no-danmaku", action="store_true", help="不连弹幕服务器")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")
    return parser.parse_args(argv)


def run_headless(args: argparse.Namespace) -> int:
    """无界面模式：阻塞到 Ctrl+C。"""
    app = build_application(data_directory=args.data_dir, port=args.port,
                            on_log=lambda message: log.info(message))
    if args.allow_headless_api:
        app.allow_headless_api(True)
    if not app.start(args.room, start_danmaku=not args.no_danmaku):
        log.error("启动失败：%s", app.http.error)
        return 1
    log.info("点歌姬 v%s 运行中：%s", __version__, app.http.url)
    log.info("主播台 %s · OBS 层 %s", app.console_url(), app.overlay_url())
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，正在退出…")
    finally:
        app.shutdown()
    return 0


def run_gui(args: argparse.Namespace) -> int:
    """界面模式：推迟 import tkinter，headless 环境不会因为缺 Tk 挂掉。

    装配（``build_application``）在这里做、不在 ``ui`` 里做 —— 界面层不该
    import 组合根，否则就是「表现层依赖装配层」的反向依赖。
    """
    try:
        from .ui.main_window import launch
    except ImportError as exc:  # pragma: no cover - 缺 Tk 的 Python
        log.error("无法加载界面（%s），试试 --headless", exc)
        return 1

    try:
        app = build_application(data_directory=args.data_dir, port=args.port,
                                on_log=lambda message: log.info(message))
    except Exception as exc:  # pragma: no cover - 启动期异常必须让人看见
        log.exception("初始化失败")
        log.error("初始化失败：%s", exc)
        return 1

    if args.allow_headless_api:
        app.allow_headless_api(True)
    if args.room:
        app.config.room_id = str(args.room).strip()
    return launch(app, args=args)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)
    log.info("点歌姬 v%s 启动", __version__)
    if args.headless:
        return run_headless(args)
    return run_gui(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
