"""HTTP 路由与处理器。

与原 ``server.py`` 的差异：

* 全局变量 ``STATE`` 没了，改成注入的 :class:`AppContext`（服务对象 + 曲库 + 配置）。
* ``/media/`` 的路径解析改成精确匹配（原 ``endswith`` 在 ``rel`` 为空时恒真，
  会把曲库第一首返回给 ``GET /media/``），Range 也修了 ``bytes=-500`` 的语义。
* 新增 ``/api/ended``：由前端上报「这一首唱完了，ID 是 xxx」，
  服务端只认当前播放项 —— 修掉开两个页面导致一首歌跳两首的问题。
* ``/api/*`` 统一先过安全校验，再读 body。
"""

from __future__ import annotations

import json
import os
import socket
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from ..constants import (
    APP_NAME,
    APP_VERSION,
    AUDIO_MIME,
    CONSOLE_QUERY,
    DEFAULT_PORT,
    MAX_BODY_BYTES,
)
from ..core.errors import DianGeJiError
from ..core.library import LocalLibrary
from ..logging_setup import get_logger
from ..paths import button_image_dir, image_dir, template_dir
from ..services.playback import PlaybackService
from ..utils.text import strip_order_prefix
from .security import is_local_request
from .views import render_console, render_overlay

log = get_logger(__name__)

MIME_PNG = "image/png"
MIME_HTML = "text/html; charset=utf-8"
MIME_JSON = "application/json; charset=utf-8"
MIME_TEXT = "text/plain; charset=utf-8"

#: 「需要 --allow-headless-api」这条提示每次启动只喊一次，别刷屏
_HEADLESS_HINTED = False


@dataclass
class AppContext:
    """处理器需要的全部依赖，一个对象传进去（便于测试时替换）。"""

    playback: PlaybackService
    library: LocalLibrary
    template_dir: Any = field(default_factory=template_dir)
    image_dir: Any = field(default_factory=image_dir)
    button_dir: Any = field(default_factory=button_image_dir)
    danmaku_state: Callable[[], dict] | None = None
    version: str = APP_VERSION
    #: 是否允许无 Origin/Referer 的客户端（本机脚本调试用），默认关
    allow_headless: bool = False


class RequestHandler(BaseHTTPRequestHandler):
    """只做分发，业务全在 :class:`AppContext` 里。"""

    server_version = f"DianGeJi/{APP_VERSION}"
    protocol_version = "HTTP/1.1"

    ctx: AppContext  # 由工厂函数注入

    # ---------------- 基础设施 ----------------

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes | str = b"",
              ctype: str = MIME_JSON) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False), MIME_JSON)

    def _forbidden(self) -> None:
        hint = ("本接口只接受本机浏览器发来的请求。脚本 / QQ 机器人 / 自动化工具"
                "请启动时加 --allow-headless-api")
        self._json({"ok": False, "error": "forbidden", "hint": hint}, 403)

    def _check_local(self, path: str) -> bool:
        """/api/* 走严格校验；静态资源不校验（本来就是给本机页面用的）。"""
        if not path.startswith("/api/"):
            return True
        if is_local_request(self.headers, allow_headless=self.ctx.allow_headless):
            return True
        has_browser_header = any(self.headers.get(name)
                                 for name in ("Origin", "Referer"))
        if has_browser_header:
            log.warning("拒绝非本机请求：%s %s（Origin/Referer 不是回环地址）",
                        self.command, path)
        else:
            global _HEADLESS_HINTED
            if not _HEADLESS_HINTED:
                _HEADLESS_HINTED = True
                log.warning(
                    "拒绝无 Origin/Referer 的请求：%s %s —— 这是默认拒绝策略。"
                    "如果你在用脚本 / QQ 机器人调接口，请改用 "
                    "`diangeji --allow-headless-api` 启动（同一次启动只需提示一次）。",
                    self.command, path)
        return False

    def _body(self) -> dict[str, Any]:
        """读 JSON 请求体；任何异常都退回空字典（接口自己会校验字段）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        if length <= 0 or length > MAX_BODY_BYTES:
            return {}
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # ---------------- 路由：GET ----------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._check_local(path):
            return self._forbidden()
        try:
            self._route_get(path)
        except DianGeJiError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover - 兜底，别把栈泄给浏览器
            log.exception("GET %s 失败", path)
            self._json({"ok": False, "error": str(exc)}, 500)

    def _route_get(self, path: str) -> None:
        ctx = self.ctx
        if path in ("/", "/index.html"):
            state = ctx.playback.snapshot()
            state["dm"] = ctx.danmaku_state() if ctx.danmaku_state else {}
            state["lib"] = len(ctx.library)
            html = render_console(ctx.template_dir, state, len(ctx.library), ctx.version)
            return self._send(200, html, MIME_HTML)

        if path in ("/overlay", "/overlay.html"):
            return self._send(200, render_overlay(ctx.template_dir, ctx.version), MIME_HTML)

        if path == "/api/state":
            ctx.playback.queue.touch()
            return self._json(self._state_payload())

        if path == "/api/library":
            return self._json({"count": len(ctx.library), "sample": ctx.library.sample(20)})

        if path.startswith("/asset/"):
            return self._asset(path[len("/asset/"):])

        if path.startswith("/media/"):
            return self._media(path[len("/media/"):])

        return self._send(404, "not found", MIME_TEXT)

    def _state_payload(self) -> dict[str, Any]:
        ctx = self.ctx
        payload = ctx.playback.snapshot()
        payload["dm"] = ctx.danmaku_state() if ctx.danmaku_state else {}
        payload["lib"] = len(ctx.library)
        return payload

    # ---------------- 静态资源 ----------------

    def _asset(self, name: str) -> None:
        """``/asset/xxx.png``：只允许图片，且只从包内目录取（挡目录穿越）。"""
        ctx = self.ctx
        decoded = urllib.parse.unquote(name)
        base = os.path.basename(decoded.replace("\\", "/"))
        if not base or not base.lower().endswith((".png", ".ico")):
            return self._send(404, "no asset", MIME_TEXT)
        for directory, ctype in ((ctx.image_dir, MIME_PNG), (ctx.button_dir, MIME_PNG),
                                 (ctx.image_dir, "image/x-icon")):
            candidate = os.path.join(str(directory), base)
            if os.path.isfile(candidate):
                if base.lower().endswith(".ico"):
                    ctype = "image/x-icon"
                return self._send(200, _read_bytes(candidate), ctype)
        return self._send(404, "no asset", MIME_TEXT)

    # ---------------- 媒体（Range 支持） ----------------

    def _media(self, rel: str) -> None:
        target = self.ctx.library.path_for(urllib.parse.unquote(rel))
        if not target or not os.path.isfile(target):
            return self._send(404, "no file", MIME_TEXT)

        size = os.path.getsize(target)
        ctype = AUDIO_MIME.get(os.path.splitext(target)[1].lower(),
                               "application/octet-stream")
        span = _parse_range(self.headers.get("Range"), size)
        if span is None:
            return self._send(416, "range not satisfiable", MIME_TEXT)
        start, end = span
        length = end - start + 1

        self.send_response(206 if self.headers.get("Range") else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if self.headers.get("Range"):
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        if self.command == "HEAD":      # HEAD 只发头，不发 body
            return

        with open(target, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(262144, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    # ---------------- 路由：POST ----------------

    def _drain(self) -> None:
        """把请求体读完丢掉。

        HTTP/1.1 默认 keep-alive，没读的 body 会被当成下一条请求的请求行，
        于是「403 之后紧接着一个 400、然后连接被掐」。非法请求也要读完。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        if 0 < length <= MAX_BODY_BYTES:
            try:
                self.rfile.read(length)
            except (OSError, ValueError):
                pass

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if not self._check_local(path):
            self._drain()
            return self._forbidden()
        data = self._body()
        try:
            self._route_post(path, data)
        except DianGeJiError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover
            log.exception("POST %s 失败", path)
            self._json({"ok": False, "error": str(exc)}, 500)

    def _route_post(self, path: str, data: dict[str, Any]) -> None:
        playback = self.ctx.playback

        if path == "/api/order":
            text = str(data.get("text") or "").strip()
            user = str(data.get("user") or "观众")
            item = playback.request(user, strip_order_prefix(text))
            return self._json({"ok": True, "item": item.to_dict()})

        if path in ("/api/skip", "/api/next"):
            return self._json({"ok": True, "state": playback.next()})

        if path == "/api/prev":
            return self._json({"ok": True, "state": playback.previous()})

        if path == "/api/ended":
            # 前端上报「唱完了」；只有当前播放项的 ID 对得上才切歌
            item_id = str(data.get("id") or "")
            changed = playback.skip_if_finished(item_id)
            return self._json({"ok": True, "advanced": changed, "state": playback.snapshot()})

        if path == "/api/pause":
            value = data.get("value") if "value" in data else None
            return self._json({"ok": True, "state": playback.set_paused(value)})

        if path == "/api/volume":
            return self._json({"ok": True, "state": playback.set_volume(data.get("value", 20))})

        if path == "/api/idle":
            value = data.get("value") if "value" in data else None
            return self._json({"ok": True, "state": playback.set_idle(value)})

        if path == "/api/remove":
            return self._json({"ok": True,
                               "state": playback.remove(str(data.get("id") or ""))})

        if path == "/api/clear":
            return self._json({"ok": True, "state": playback.clear()})

        if path == "/api/search":
            keyword = str(data.get("q") or "").lower()
            hits = self.ctx.library.search(keyword, limit=20)
            return self._json({"hits": hits})

        return self._json({"ok": False, "error": "unknown"}, 404)

    # ---------------- 其他方法 ----------------

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """解析 ``Range`` 头，返回 ``(start, end)``（含两端）。

    * 无 Range → 整个文件
    * ``bytes=100-`` → 100 到末尾
    * ``bytes=100-200``
    * ``bytes=-500`` → **最后** 500 字节（原实现错当成 0-500）
    * 越界 → ``None``（调用方回 416）
    """
    if size <= 0:
        return None
    if not header or not header.startswith("bytes="):
        return 0, size - 1
    spec = header[6:].strip()
    if "," in spec:  # 多段不支持，退化成一整块
        return 0, size - 1
    start_text, sep, end_text = spec.partition("-")
    if not sep:
        return 0, size - 1
    try:
        if not start_text:
            if not end_text:
                return 0, size - 1
            tail = int(end_text)
            if tail <= 0:
                return None
            start = max(0, size - tail)
            return start, size - 1
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    except ValueError:
        return 0, size - 1
    if start < 0 or start >= size:
        return None
    if end < start:
        end = size - 1
    return start, min(end, size - 1)


def make_handler(ctx: AppContext) -> type[RequestHandler]:
    """把上下文绑进处理器类（``BaseHTTPRequestHandler`` 每个连接新建实例）。"""
    class BoundHandler(RequestHandler):
        pass

    BoundHandler.ctx = ctx
    BoundHandler.__name__ = "BoundRequestHandler"
    return BoundHandler


class AppServer(ThreadingHTTPServer):
    """便于测试：``serve()`` 在后台线程跑，``shutdown()`` 干净停。

    注意 ``allow_reuse_address`` 在 Windows 上是个陷阱：``SO_REUSEADDR`` 允许
    **两个 socket 绑同一个端口**，于是「端口被占用」永远检测不出来，两个点歌姬
    实例会抢同一个端口而都不报错。Windows 必须换成 ``SO_EXCLUSIVEADDRUSE``；
    POSIX 上 ``SO_REUSEADDR`` 的语义是「跳过 TIME_WAIT」，那才是我们想要的。
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def __init__(self, address: tuple[str, int], handler: type[RequestHandler]) -> None:
        if os.name == "nt":
            self.allow_reuse_address = False
        super().__init__(address, handler)
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:  # pragma: no cover - 极老系统没有这个选项
                pass
        self.stopping = False


def build_server(ctx: AppContext, port: int = DEFAULT_PORT,
                 host: str = "127.0.0.1") -> AppServer:
    """按上下文建一个可运行的服务器对象（不启动）。"""
    return AppServer((host, port), make_handler(ctx))


__all__ = [
    "AppContext",
    "AppServer",
    "RequestHandler",
    "build_server",
    "make_handler",
    "MIME_HTML",
    "CONSOLE_QUERY",
    "APP_NAME",
]
