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
from typing import TYPE_CHECKING, Any, Callable

from ..constants import (
    APP_VERSION,
    AUDIO_MIME,
    DEFAULT_PORT,
    MAX_BODY_BYTES,
)
from ..core.errors import DianGeJiError
from ..logging_setup import get_logger
from ..paths import button_image_dir, image_dir, template_dir
from ..services.playback import PlaybackService
from ..utils.text import strip_order_prefix
from .security import is_host_allowed, is_local_request

if TYPE_CHECKING:
    # ``LocalLibrary`` 只用于类型标注，运行时不需要 —— 放进 TYPE_CHECKING
    # 后 ``web`` 层不再真正依赖 ``core.library`` 这个具体类，只依赖
    # ``core.errors.DianGeJiError``（那是契约的一部分）。
    from ..core.library import LocalLibrary
from .views import render_console, render_overlay

log = get_logger(__name__)

MIME_PNG = "image/png"
MIME_HTML = "text/html; charset=utf-8"
MIME_JSON = "application/json; charset=utf-8"
MIME_TEXT = "text/plain; charset=utf-8"

#: 「需要 --allow-headless-api」这条提示每次启动只喊一次，别刷屏
_HEADLESS_HINTED = False

#: 客户端中途断开时该吞掉的异常。
#:
#: 注意 ``ConnectionAbortedError`` 是 ``BrokenPipeError`` / ``ConnectionResetError``
#: 的**兄弟**，不是子类 —— 只写前两个接不住它。而 Windows 上播放器切歌、
#: 关页面、OBS 重载抛的恰恰是 ``ConnectionAbortedError [WinError 10053]``，
#: 漏掉它会让每次断开都刷一条 traceback，最后只回一个畸形的 500。
#: 统一的父类 ``ConnectionError`` 最稳，这三者都是它的子类。
CLIENT_GONE = (ConnectionError,)

#: 500 一律回固定文案。``str(exc)`` 里可能是绝对路径、临时文件名之类的
#: 服务端细节 —— 那属于日志，不属于交出给浏览器的响应体。
INTERNAL_ERROR = "服务器内部错误（详情见日志）"

#: /api/state 每次轮询最多回多少条队列。
#: 前端只渲染前几十条，把几百条全序列化既慢又占带宽，而且是在队列锁里做的。
STATE_QUEUE_LIMIT = 50


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
            except CLIENT_GONE:
                pass

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False), MIME_JSON)

    def _domain_error(self, exc: DianGeJiError) -> None:
        """把领域异常翻成响应。

        ``http_status`` 决定 400 还是 500 —— 别再写死 400，
        「模板缺失」交给用户看 400 是没有意义的，他改不了。
        500 的详情只进日志，不交给浏览器。
        """
        status = int(getattr(exc, "http_status", 400) or 400)
        if status >= 500:
            log.error("业务异常（服务端）: %s", exc, exc_info=True)
            self._json({"ok": False, "error": INTERNAL_ERROR}, status)
        else:
            self._json({"ok": False, "error": str(exc)}, status)

    def _forbidden(self) -> None:
        hint = ("本接口只接受本机浏览器发来的请求。脚本 / QQ 机器人 / 自动化工具"
                "请启动时加 --allow-headless-api")
        self._json({"ok": False, "error": "forbidden", "hint": hint}, 403)

    def _check_local(self, path: str) -> bool:
        """两道不同的校验，别合并：

        1. **所有**请求先验 ``Host`` 是不是回环 —— 挡 DNS rebinding。
           攻击者把 ``evil.com`` 解析到 127.0.0.1 后，浏览器眼里它和本地服务
           同源，JS 就能 ``fetch('/')`` 把整个队列读走。这一步跟 ``/api/``
           无关，页面和图片同样是数据（观众昵称、点了什么歌）。
        2. ``/api/*`` 再追加 ``Origin``/``Referer`` 校验 —— 挡跨站请求。
           静态资源不查这个：浏览器加载 ``<img>`` / ``<audio>`` 不带这些头。
        """
        if not is_host_allowed(self.headers):
            log.warning("拒绝非回环 Host 的请求：%s %s（可疑 Host: %s）",
                        self.command, path, self.headers.get("Host"))
            return False
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
            self.close_connection = True
            return {}
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            # 不读就返回会把 body 留在 socket 里，下一条请求会把它当成请求行
            # （实测会得到 "501 Unsupported method ('{\"text\":\"x\"}GET')"）。
            # 这里必须关连接，宁可牺牲一次 keep-alive 也不能污染下一条请求。
            self.close_connection = True
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
            self._domain_error(exc)
        except Exception:  # pragma: no cover - 兜底，别把栈泄给浏览器
            log.exception("GET %s 失败", path)
            self._json({"ok": False, "error": INTERNAL_ERROR}, 500)

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
        # 轮询路径：不带日志、队列截断。日志框有 ``(s.log || [])`` 兜底，
        # 少了字段不会出错；完整日志由 / 首屏注入。
        payload = ctx.playback.snapshot(include_log=False, limit=STATE_QUEUE_LIMIT)
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
        # MIME 由扩展名决定，不需要按目录指定 —— 原来第三项又回到 image_dir
        # 只换个 content-type，等于把同一个目录白找一遍，永远命中不了新文件。
        ctype = "image/x-icon" if base.lower().endswith(".ico") else MIME_PNG
        for directory in (ctx.image_dir, ctx.button_dir):
            candidate = os.path.join(str(directory), base)
            if os.path.isfile(candidate):
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
        range_header = self.headers.get("Range")
        span = _parse_range(range_header, size)
        if span is None:
            return self._send(416, "range not satisfiable", MIME_TEXT)
        start, end = span
        length = end - start + 1
        partial = _is_partial(range_header, span, size)

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
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
                except CLIENT_GONE:
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
            self.close_connection = True
            return
        if length <= 0:
            return
        if length > MAX_BODY_BYTES:
            # 太大了不敢读（可能等半天），但也不能就这么走 —— 残留字节会被
            # 下一条请求当成请求行解析。直接标记连接不可复用，让服务端关掉它。
            self.close_connection = True
            return
        try:
            self.rfile.read(length)
        except (OSError, ValueError):
            self.close_connection = True

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if not self._check_local(path):
            self._drain()
            return self._forbidden()
        data = self._body()
        try:
            self._route_post(path, data)
        except DianGeJiError as exc:
            self._domain_error(exc)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except Exception:  # pragma: no cover
            log.exception("POST %s 失败", path)
            self._json({"ok": False, "error": INTERNAL_ERROR}, 500)

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

    * 无 Range / **语法非法** → 整个文件（按 RFC 7233，非法 Range 应当忽略）
    * ``bytes=100-`` → 100 到末尾
    * ``bytes=100-200``
    * ``bytes=-500`` → **最后** 500 字节（原实现错当成 0-500）
    * 越界 → ``None``（调用方回 416）

    返回值不带「这是不是真的 Range」的信息 —— 那由 :func:`_range_is_satisfiable`
    和调用方共同判断，别在这里混进 HTTP 语义。
    """
    if size <= 0:
        return None
    if not header:
        return 0, size - 1
    if not header.startswith("bytes="):
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
        # 语法非法：按 RFC 忽略这个头，回 200 + 整个文件。
        # 注意不能返回 None（那是 416 的意思）。
        return 0, size - 1
    if start < 0 or start >= size:
        return None
    if end < start:
        end = size - 1
    return start, min(end, size - 1)


def _is_partial(header: str | None, span: tuple[int, int], size: int) -> bool:
    """这个响应该发 206 吗？

    只有「客户端真的请求了部分内容」才发 206。原实现是
    ``206 if self.headers.get("Range") else 200`` —— 只要带了 Range 头就 206，
    于是 ``bytes=abc-def`` 这种畸形头会得到一个「声称是部分内容、实际是完整
    文件、Content-Range 覆盖全文件」的响应。Safari / iOS 的 ``<audio>`` 拿到
    这种响应会判定不可 seek 或直接中断。
    """
    if not header or not header.startswith("bytes="):
        return False
    return span != (0, size - 1)


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
    "MIME_JSON",
    "MIME_PNG",
    "MIME_TEXT",
]
