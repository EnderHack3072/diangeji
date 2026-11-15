"""B 站弹幕客户端：连接 / 鉴权 / 心跳 / 重连 / 去重。

和原版的行为差异（都是修 bug，不是换设计）：

* ``uid`` 在 ``__init__`` 里就解析好，``_handle_one`` 不再依赖 ``self.uid`` 是否存在。
* 去重表改成**有上限的 OrderedDict**，不再是「超 60 条整个 clear」——
  原版清空后，正在刷屏的人可以立刻重复触发。
* 连接状态、最近弹幕都通过 :class:`DanmakuState` 暴露，界面直接读，不再用 ``getattr`` 猜。
* 回调里抛异常不会影响收包循环（原版只包了 ``on_order``，日志回调没包）。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from ..constants import BILI_LIVE_URL, UA_DESKTOP
from ..logging_setup import get_logger
from ..utils.webhttp import get_json
from ..utils.ws import WSClient, WebSocketError
from . import protocol
from .commands import Order, parse_order
from .wbi import WBISigner

log = get_logger(__name__)

ROOM_INIT = "https://api.live.bilibili.com/room/v1/Room/room_init?id={room}"
DANMU_INFO = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"

#: 重连退避（秒）
RECONNECT_DELAYS = (3, 6, 12, 30, 60)
#: 被风控后的冷却（秒）
RATELIMIT_COOLDOWN = 1800
#: 弹幕服务器 host 缓存有效期
HOST_CACHE_TTL = 6 * 3600
#: 同一人同一句的去重窗口
DEDUP_WINDOW = 3.0
DEDUP_SIZE = 256

OrderCallback = Callable[[str, Order], None]
LogCallback = Callable[[str], None]


class RateLimited(Exception):
    """接口被风控（-352 或拿不到 token），需要长时间冷却。"""


@dataclass
class DanmakuState:
    """可读连接状态，界面和 HTTP 接口都从这里取。"""

    connected: bool = False
    room: str | None = None
    last_danmaku: str = ""
    count: int = 0
    uid: int = 0
    last_error: str = ""


class DanmakuClient:
    """一个直播间的弹幕连接。"""

    def __init__(self, room_id: str, cookie: str = "",
                 on_order: OrderCallback | None = None,
                 on_log: LogCallback | None = None,
                 cache_path=None) -> None:
        self.room_id = str(room_id).strip()
        self.cookie = cookie or ""
        self.on_order = on_order
        self.on_log = on_log or (lambda message: None)
        self.cache_path = cache_path
        self.state = DanmakuState()
        self.state.uid = self._uid_from_cookie()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._recent: OrderedDict[tuple, float] = OrderedDict()
        self._signer = WBISigner()

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="danmaku", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---------------- 内部：主循环 ----------------

    def _log(self, message: str) -> None:
        try:
            self.on_log(message)
        except Exception:  # pragma: no cover - 日志回调自己炸了不能拖垮连接
            pass
        log.info(message)

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            session_ok = False
            try:
                self._session()
                session_ok = True
            except RateLimited as exc:
                self.state.connected = False
                self.state.last_error = str(exc)
                self._log(f"弹幕暂时连不上：{exc}，5 分钟后再试")
                self._sleep(RATELIMIT_COOLDOWN)
                continue
            except Exception as exc:
                self.state.connected = False
                self.state.last_error = str(exc)
                self._log(f"弹幕连接异常：{exc}")
            finally:
                self.state.connected = False

            if self._stop.is_set():
                break
            attempt = 0 if session_ok else min(attempt + 1, len(RECONNECT_DELAYS) - 1)
            self._sleep(RECONNECT_DELAYS[attempt])

    def _sleep(self, seconds: float) -> None:
        """可被 ``stop()`` 打断的睡眠。"""
        end = time.time() + seconds
        while time.time() < end and not self._stop.is_set():
            time.sleep(0.2)

    # ---------------- 内部：一次会话 ----------------

    def _session(self) -> None:
        real_room, live = self._room_init()
        self.state.room = str(real_room)
        self._log(f"房间 {real_room}（{'直播中' if live else '未开播'}）")

        hosts, token = self._danmu_info(real_room)
        ws = WSClient(f"wss://{hosts[0]}/sub", origin=BILI_LIVE_URL)
        ws.connect(timeout=10)
        try:
            ws.send(protocol.auth_payload(real_room, token, uid=self.state.uid))
            code = self._wait_auth(ws)
            if code != 0:
                raise RuntimeError(f"鉴权被拒 code={code}")
            self.state.connected = True
            self._log("已连接弹幕服务器" if self.state.uid else "已连接（游客）")
            self._pump(ws)
        finally:
            self.state.connected = False
            ws.close()

    def _pump(self, ws: WSClient) -> None:
        last_beat = 0.0
        while not self._stop.is_set():
            if time.time() - last_beat > 25:
                ws.send(protocol.heartbeat_payload())
                last_beat = time.time()
            ws.set_timeout(10)
            try:
                opcode, payload = ws.recv()
            except socket.timeout:
                continue
            except (WebSocketError, OSError):
                break
            if opcode == protocol.OP_CLOSE:
                break
            if opcode in (protocol.OP_MESSAGE, protocol.OP_HEARTBEAT_REPLY):
                self._handle_payload(payload)

    def _wait_auth(self, ws: WSClient) -> int:
        """等服务端回鉴权结果。返回 0 表示通过。"""
        deadline = time.time() + 8
        while time.time() < deadline and not self._stop.is_set():
            ws.set_timeout(max(1.0, deadline - time.time()))
            try:
                opcode, payload = ws.recv()
            except (socket.timeout, WebSocketError, OSError):
                return -1
            if opcode == protocol.OP_CLOSE:
                return self._auth_code(payload)
            if len(payload) >= protocol.HEADER_SIZE:
                # 服务端用二进制帧（ws op=2）回鉴权结果，里面是带 16 字节头的弹幕包
                for packet in protocol.iter_packets(payload):
                    if packet.opcode == protocol.OP_AUTH_REPLY:
                        return self._auth_code(packet.body)
        return -1

    @staticmethod
    def _auth_code(payload: bytes) -> int:
        try:
            return int(json.loads(payload.decode("utf-8")).get("code", -1))
        except (UnicodeDecodeError, ValueError, AttributeError):
            return -1

    def _handle_payload(self, payload: bytes) -> None:
        try:
            for message in protocol.iter_messages(payload):
                self._handle_message(message)
        except Exception as exc:  # pragma: no cover - 单包异常不影响后续
            log.debug("解析弹幕包失败：%s", exc)

    def _handle_message(self, message: dict) -> None:
        cmd = str(message.get("cmd") or "")
        if not cmd.startswith("DANMU_MSG"):
            return
        info = message.get("info") or []
        try:
            text = str(info[1])
            user = str(info[2][1]) if len(info) > 2 and len(info[2]) > 1 else "观众"
            uid = int(info[2][0]) if len(info) > 2 else 0
        except (IndexError, TypeError, ValueError):
            return

        self.state.count += 1
        if self.state.uid and uid == self.state.uid:
            return  # 自己的弹幕不算
        self.state.last_danmaku = f"{user}: {text}"

        if self._is_duplicate(uid, text):
            return
        order = parse_order(text)
        if order is None:
            return
        if self.on_order is not None:
            try:
                self.on_order(user, order)
            except Exception as exc:  # pragma: no cover
                self._log(f"处理点歌失败：{exc}")

    def _is_duplicate(self, uid: int, text: str) -> bool:
        """同一人同一句在窗口期内只算一次（断线重连会把同一条推两次）。"""
        key = (uid, (text or "").strip())
        now = time.time()
        if now - self._recent.get(key, 0.0) < DEDUP_WINDOW:
            return True
        self._recent[key] = now
        self._recent.move_to_end(key)
        while len(self._recent) > DEDUP_SIZE:
            self._recent.popitem(last=False)
        return False

    # ---------------- 内部：接口 ----------------

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": UA_DESKTOP, "Referer": BILI_LIVE_URL, "Cookie": self.cookie}

    def _room_init(self) -> tuple[int, int]:
        data = get_json(ROOM_INIT.format(room=self.room_id), headers=self._headers(), timeout=8)
        if not data or data.get("code") != 0:
            code = (data or {}).get("code")
            raise RuntimeError(f"房间 {self.room_id} 无效（code={code}）")
        body = data.get("data") or {}
        return int(body.get("room_id") or self.room_id), int(body.get("live_status") or 0)

    def _danmu_info(self, real_room: int) -> tuple[list[str], str]:
        cached = self._read_host_cache()
        info = self._danmu_info_once(real_room)

        if str(info.get("code")) == "-352":
            # 风控多半是 wbi key 过期：作废缓存后**立刻重签一次**再试，
            # 而不是直接进 30 分钟冷却（那等于白等）。
            self._signer.invalidate()
            self._log("wbi 签名可能过期，重签后重试")
            info = self._danmu_info_once(real_room)

        data = info.get("data") or {}
        hosts = [str(h["host"]) for h in (data.get("host_list") or []) if h.get("host")]
        token = str(data.get("token") or "")
        if hosts and token:
            self._write_host_cache(hosts, token, real_room)
            return hosts, token

        if str(info.get("code")) == "-352":
            if cached and cached.get("fresh"):
                self._log("接口受限，沿用上次的弹幕服务器")
                return cached["hosts"], cached["token"]
            raise RateLimited("-352 风控，等冷却")

        self._log(f"取弹幕服务器受限（code={info.get('code')}）")
        if cached and cached.get("fresh"):
            self._log("沿用上次的弹幕服务器")
            return cached["hosts"], cached["token"]
        raise RateLimited("拿不到弹幕服务器 token")

    def _danmu_info_once(self, real_room: int) -> dict:
        url = self._signer.sign(DANMU_INFO, {"id": str(real_room), "type": "0"}, self.cookie)
        return get_json(url, headers=self._headers(), timeout=8) or {}

    def _read_host_cache(self) -> dict | None:
        if not self.cache_path:
            return None
        try:
            with open(self.cache_path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        hosts = data.get("hosts") or []
        token = data.get("token") or ""
        if not hosts or not token:
            return None
        try:
            age = time.time() - float(data.get("ts") or 0)
        except (TypeError, ValueError):
            age = HOST_CACHE_TTL + 1
        data["fresh"] = age < HOST_CACHE_TTL and str(data.get("room")) == str(self.state.room or data.get("room"))
        return data

    def _write_host_cache(self, hosts: list[str], token: str, room: int) -> None:
        if not self.cache_path:
            return
        try:
            with open(self.cache_path, "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "hosts": hosts, "token": token, "room": room}, fh)
        except OSError:
            pass

    # ---------------- 内部：杂项 ----------------

    def _uid_from_cookie(self) -> int:
        for part in self.cookie.split(";"):
            key, _, value = part.strip().partition("=")
            if key == "DedeUserID" and value.isdigit():
                return int(value)
        return 0
