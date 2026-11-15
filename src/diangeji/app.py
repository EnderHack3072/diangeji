"""应用装配（composition root）。

**只有这里知道「谁住在哪」**：把路径、配置、曲库、音源、服务、HTTP 装成一个
:class:`Application`。界面拿到它就能干活，不需要自己 new 一堆东西。

依赖方向是单向的：``ui`` → ``Application`` → ``services`` → ``core``/``sources``/``live``。
没有任何模块反向 import ``ui``。
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .auth.browser import BrowserLogin
from .auth.store import BILI_COOKIE, CookieStore
from .config import Config
from .constants import APP_VERSION, CONSOLE_QUERY, DEFAULT_PORT
from .core.library import LocalLibrary
from .core.queue import PlayQueue
from .live.client import DanmakuClient
from .live.commands import Order
from .logging_setup import get_logger, setup as setup_logging
from .paths import button_image_dir, data_dir, image_dir, template_dir
from .services.library_service import LibraryService
from .services.playback import PlaybackService
from .sources.local_source import LocalSource
from .sources.qqmusic import QQMusicConfig, QQMusicSource
from .sources.registry import SourceRegistry, default_registry
from .web.routes import AppContext
from .web.server import HttpService

log = get_logger(__name__)


@dataclass
class Application:
    """一个运行中的点歌姬实例。"""

    data_dir: Path
    config: Config
    library: LocalLibrary
    library_service: LibraryService
    qq_source: QQMusicSource
    registry: SourceRegistry
    playback: PlaybackService
    cookies: CookieStore
    browser: BrowserLogin
    http: HttpService
    danmaku: DanmakuClient | None = None
    on_log: Callable[[str], None] = field(default=lambda message: None)

    #: 点歌解析队列：收包线程只投递，单 worker 顺序消费（见 ``_handle_order``）
    _order_queue: queue.Queue = field(default_factory=queue.Queue, repr=False)
    _order_stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _order_worker_thread: threading.Thread | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._order_worker_thread = threading.Thread(
            target=self._order_worker, name="order-worker", daemon=True)
        self._order_worker_thread.start()

    # ---------------- 日志 ----------------

    def log(self, message: str) -> None:
        """同时进文件日志和界面日志框。"""
        log.info(message)
        try:
            self.on_log(message)
        except Exception:  # pragma: no cover - 界面没准备好时忽略
            pass

    # ---------------- 弹幕 ----------------

    @property
    def bili_cookie(self) -> str:
        return self.cookies.read(BILI_COOKIE)

    def start_danmaku(self, room_id: str) -> DanmakuClient:
        """连直播间弹幕。重复调用不会起第二个连接。"""
        if self.danmaku is not None and self.danmaku.alive:
            return self.danmaku
        client = DanmakuClient(
            room_id=room_id,
            cookie=self.bili_cookie,
            on_order=self._handle_order,
            on_log=self.log,
            cache_path=self.data_dir / "danmu_hosts.json",
        )
        self.danmaku = client
        client.start()
        return client

    def stop_danmaku(self) -> None:
        if self.danmaku is not None:
            self.danmaku.stop()
        self.danmaku = None

    def _handle_order(self, user: str, order: Order) -> None:
        """弹幕点歌 → 入队。

        这里**只投递不解析**：解析一首歌要走 QQ 音乐搜索 + 取直链（最坏 24 秒），
        而弹幕连接的心跳窗口只有 25 秒 —— 在收包线程里同步跑，一次慢查询就能把
        连接拖死，直播间人多时更是串行排队。
        """
        self.log(f"[点歌] {user} → {order.keyword}")
        self._order_queue.put((user, order))

    def _order_worker(self) -> None:
        """单个后台线程按顺序解析点歌，保证入队顺序与弹幕顺序一致。"""
        while not self._order_stop.is_set():
            try:
                user, order = self._order_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if order is None:      # 停止哨兵
                break
            try:
                self.playback.request(user, order.keyword)
            except Exception as exc:  # pragma: no cover - 单条失败不该拖垮 worker
                log.exception("入队失败")
                self.log(f"入队失败：{exc}")

    def danmaku_state(self) -> dict:
        """给 HTTP 接口的弹幕状态。"""
        client = self.danmaku
        if client is None:
            return {"connected": False, "room": None, "last": "", "count": 0}
        state = client.state
        return {
            "connected": state.connected,
            "room": state.room,
            "last": state.last_danmaku,
            "count": state.count,
        }

    # ---------------- 登录态 ----------------

    def refresh_sources(self) -> None:
        """cookie 变了以后让 QQ 音源重新可用。

        原来这个函数只打了一行日志，docstring 却承诺「让音源重新可用」——
        ``cookie()`` 确实每次实时读文件，所以 ``enabled()`` 会立刻正确；
        真正需要清的是**和登录态绑定的缓存**（直链、红心歌单），
        不清就会用上一个号的直链去播新号点的歌。
        """
        self.qq_source.invalidate()
        log.info("QQ 音乐登录态：%s（已清空直链/歌单缓存）",
                 "已登录" if self.qq_source.enabled() else "未登录")

    # ---------------- 生命周期 ----------------

    def start(self, room_id: str | None = None, *, start_danmaku: bool = True) -> bool:
        """扫曲库 → 起 HTTP → 连弹幕。返回 HTTP 是否起来了。"""
        room = (room_id or self.config.room_id or "").strip()
        self.config.room_id = room
        self.config.save()

        count = self.library_service.scan(self.config.local_dirs)
        self.log(f"曲库扫描完成：{count} 首")

        if not self.http.start():
            self.log(self.http.error)
            return False
        self.log(f"服务已启动 · {self.http.url}")

        if start_danmaku:
            if not room.isdigit():
                self.log("房间号不是纯数字，弹幕没连（可以先在主播台手动点歌）")
            elif not self.bili_cookie:
                self.log("还没登录 B 站，弹幕收不到。点「① 扫码登录 B 站」。")
            else:
                self.start_danmaku(room)
        return True

    def shutdown(self) -> None:
        """关程序前的收尾：停 worker、停弹幕、停服务、尽力让页面先暂停。"""
        self._order_stop.set()
        self._order_queue.put(("", None))       # 唤醒阻塞在 get 上的 worker
        worker = self._order_worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=2)
        self.stop_danmaku()
        try:
            self.playback.set_paused(True)
        except Exception:  # pragma: no cover
            pass
        self.http.stop()

    def console_url(self, mute: bool = True) -> str:
        return self.http.url + (CONSOLE_QUERY if mute else "")

    def overlay_url(self) -> str:
        return self.http.url + "overlay"

    def allow_headless_api(self, value: bool = True) -> None:
        """放开无 Origin 的客户端（本机脚本、QQ 机器人转发）。"""
        self.http.ctx.allow_headless = value


def build_application(data_directory: Path | None = None,
                      port: int = DEFAULT_PORT,
                      on_log: Callable[[str], None] | None = None,
                      config: Config | None = None,
                      with_logging: bool = True) -> Application:
    """组装一个可运行的 :class:`Application`。测试里可以传临时目录和 0 端口。"""
    data = Path(data_directory or data_dir())
    data.mkdir(parents=True, exist_ok=True)

    if with_logging:
        setup_logging()

    cfg = (config or Config.load(data / "config.json")).normalized()

    library = LocalLibrary()
    library_service = LibraryService(library, data)

    cookies = CookieStore(data)
    qq_source = QQMusicSource(QQMusicConfig(data_dir=data))
    local_source = LocalSource(library)
    registry = default_registry(local_source, qq_source)

    playback = PlaybackService(
        queue=_new_queue(cfg),
        registry=registry,
        idle_pool=_default_idle_pool(),
        favorites=qq_source.favorites,
    )

    browser = BrowserLogin(profile_dir=data / "_edgeprofile", data_dir=data)

    ctx = AppContext(
        playback=playback,
        library=library,
        template_dir=template_dir(),
        image_dir=image_dir(),
        button_dir=button_image_dir(),
        danmaku_state=lambda: {"connected": False, "room": None, "last": "", "count": 0},
        version=APP_VERSION,
    )
    http = HttpService(ctx=ctx, port=port)

    app = Application(
        data_dir=data,
        config=cfg,
        library=library,
        library_service=library_service,
        qq_source=qq_source,
        registry=registry,
        playback=playback,
        cookies=cookies,
        browser=browser,
        http=http,
        on_log=on_log or (lambda message: None),
    )
    ctx.danmaku_state = app.danmaku_state

    # 内置空闲歌单纯在源码里写死太硬，允许数据目录覆盖
    pool_file = data / "idle_pool.txt"
    if pool_file.is_file():
        try:
            lines = [line.strip() for line in pool_file.read_text(encoding="utf-8").splitlines()]
            custom = [line for line in lines if line and not line.startswith("#")]
            if custom:
                playback.set_idle_pool(custom)
        except OSError as exc:
            log.warning("自定义空闲歌单读取失败：%s", exc)
    return app


def _new_queue(config: Config) -> PlayQueue:
    return PlayQueue(volume=config.volume, idle=config.idle_enabled)


def _default_idle_pool() -> list[str]:
    """内置空闲歌单（``点歌姬数据/idle_pool.txt`` 可覆盖）。"""
    return IDLE_DEFAULT


IDLE_DEFAULT = """周深 大鱼
朴树 平凡之路
赵雷 成都
李荣浩 年少有为
毛不易 消愁
陈奕迅 十年
张学友 吻别
任然 飞鸟和蝉
汪苏泷 有点甜
林俊杰 江南
孙燕姿 遇见
田馥甄 小幸运
梁静茹 勇气
刘若英 后来
光良 童话
周杰伦 晴天
王菲 红豆
邓紫棋 泡沫
薛之谦 演员
许巍 蓝莲花""".strip().splitlines()
