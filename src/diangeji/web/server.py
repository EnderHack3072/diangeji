"""HTTP 服务的生命周期封装：起服务、拿端口、干净停机。

界面只跟这个类打交道：``service.start()`` / ``service.stop()`` /
``service.port`` / ``service.error``。
"""

from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass

from ..constants import DEFAULT_PORT
from ..logging_setup import get_logger
from .routes import AppContext, AppServer, build_server

log = get_logger(__name__)


@dataclass
class HttpService:
    """后台线程跑的本地 HTTP 服务。"""

    ctx: AppContext
    port: int = DEFAULT_PORT
    host: str = "127.0.0.1"

    def __post_init__(self) -> None:
        self._server: AppServer | None = None
        self._thread: threading.Thread | None = None
        self.error: str = ""
        self._lock = threading.Lock()

    # ---------------- 生命周期 ----------------

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self.error)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> bool:
        """启动服务。端口被占时返回 ``False`` 并写入 ``error``。"""
        with self._lock:
            if self.running:
                return True
            try:
                self._server = build_server(self.ctx, self.port, self.host)
            except OSError as exc:
                self.error = f"端口 {self.port} 被占用：{exc}"
                log.error(self.error)
                return False
            except Exception as exc:  # pragma: no cover
                self.error = f"服务启动失败：{exc}"
                log.exception("HTTP 服务启动失败")
                return False

            self.port = self._server.server_address[1]
            self._thread = threading.Thread(
                target=self._serve, name="http", daemon=True)
            self._thread.start()
            self.error = ""
            log.info("HTTP 服务已启动：%s", self.url)
            return True

    def _serve(self) -> None:
        # 必须抓本地引用：stop() 会先把 self._server 置空，
        # 之后 finally 再去读就是 None，线程会带着 AttributeError 死掉。
        server = self._server
        if server is None:  # pragma: no cover - 理论不可达
            return
        try:
            server.serve_forever(poll_interval=0.3)
        except Exception as exc:  # pragma: no cover
            self.error = f"服务运行异常：{exc}"
            log.exception("HTTP 服务异常退出")
        finally:
            try:
                server.server_close()
            except OSError:
                pass

    def stop(self, timeout: float = 3.0) -> None:
        """停服务。可重复调用。"""
        with self._lock:
            server, thread = self._server, self._thread
            self._server, self._thread = None, None
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # pragma: no cover
                pass
            try:
                server.server_close()
            except OSError:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    # ---------------- 工具 ----------------

    @staticmethod
    def port_available(port: int, host: str = "127.0.0.1") -> bool:
        """端口是否空闲（启动前自检，给界面提示用）。

        **必须用 ``SO_EXCLUSIVEADDRUSE``，不能用 ``SO_REUSEADDR``。**
        Windows 上 ``SO_REUSEADDR`` 允许 bind 一个已经被别人占着的端口，
        于是「端口被占用」这个探针唯一要回答的问题，答案永远是「空闲」。
        而 :class:`AppServer` 绑定时用的是独占语义 —— 探针和真实绑定
        得用同一套，否则探针就是个假保险。
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if os.name == "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind((host, port))
                return True
            except OSError:
                return False

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """等端口真的开始接受连接。

        原来的判据 ``self.running and not port_available(...)`` 是反的：
        服务起来了 → 端口被自己占着 → ``port_available`` 返回 False →
        ``not False`` = True → 立刻返回 True，什么都没验证。

        现在改成真的去连一下：连得上就说明在服务。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.running:
                return False
            try:
                with socket.create_connection((self.host, self.port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.05)
        return False
