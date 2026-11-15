"""回归守卫：把外审发现的每个坑钉成一条会失败的测试。

这里的每一条都对应一个**真实发生过的 bug**，不是凑覆盖率。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from diangeji import paths
from diangeji.app import build_application
from diangeji.core.models import QueueItem
from diangeji.core.queue import PlayQueue
from diangeji.utils.ws import WSClient, WebSocketError


def template(name: str) -> str:
    return (paths.template_dir() / name).read_text(encoding="utf-8")


class TestPlayerSyncById:
    """同一首歌连点两次，播放器必须能播第二首。

    原实现用 ``path`` 判断「换歌了」，而两首同源曲目的 path 完全相同，
    于是第二个分支进不去：``curId`` 还是上一首的、``endedSent`` 还是 true，
    第二首的 ended 被丢弃 → 播放器永久卡在结尾。改用 id 判断。
    """

    @pytest.mark.parametrize("name", ["index.html", "overlay.html"])
    def test_sync_compares_id_not_path(self, name):
        html = template(name)
        # 必须有按 id 判断的分支
        assert "wantId !== curId" in html or "np.id !== curId" in html, \
            f"{name} 没有按 id 判断换歌"

    @pytest.mark.parametrize("name", ["index.html", "overlay.html"])
    def test_no_path_based_switch_detection(self, name):
        html = template(name)
        # 旧的「按 path 判断」写法不能复活
        assert "want !== cur" not in html
        assert "cur = want" not in html

    def test_two_items_same_path_have_different_ids(self):
        """前提：同源曲目 path 相同但 id 必不同。"""
        a = QueueItem(keyword="晴天", title="晴天", path="/music/晴天.mp3")
        b = QueueItem(keyword="晴天", title="晴天", path="/music/晴天.mp3")
        assert a.path == b.path
        assert a.id != b.id


class TestHeadOndMedia:
    """HEAD /media/x 不能把整段音频当 body 发出去。"""

    def test_head_media_has_no_body(self, data_dir, audio_tree):
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        app.library.scan([str(audio_tree)])
        app.http.start()
        try:
            name = next(iter(app.library.entries()))["path"]
            rel = Path(name).name
            url = f"http://127.0.0.1:{app.http.port}/media/{urllib.parse.quote(rel)}"
            request = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read()
                length = int(response.headers["Content-Length"])
            assert body == b"", "HEAD 竟然返回了 body"
            assert length > 0, "Content-Length 应当报告真实长度"
        finally:
            app.http.stop()


class TestForbiddenDrainsBody:
    """403 之后连接不能因为残留 body 变成 400。"""

    def test_post_forbidden_then_next_request_survives(self, data_dir):
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        app.http.start()
        try:
            conn = socket.create_connection(("127.0.0.1", app.http.port), timeout=5)
            payload = json.dumps({"user": "x", "text": "点歌 晴天"}).encode()
            # 带外部 Origin + 一个挺大的 body
            head = (
                "POST /api/order HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Origin: http://evil.example\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Connection: keep-alive\r\n\r\n"
            ).encode()
            conn.sendall(head + payload)

            # 第一条响应必须是 403
            first = conn.recv(4096)
            assert b"403" in first.split(b"\r\n")[0], first[:120]

            # 同一条连接上再发一个 GET，必须还能正常拿到 200
            conn.sendall(b"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                         b"Origin: http://127.0.0.1\r\nConnection: close\r\n\r\n")
            rest = b""
            conn.settimeout(5)
            while True:
                try:
                    chunk = conn.recv(65536)
                except (TimeoutError, socket.timeout):
                    break
                if not chunk:
                    break
                rest += chunk
            conn.close()
            assert b"200" in rest.split(b"\r\n")[0], \
                f"第二条请求没拿到 200，收到：{rest[:200]}"
        finally:
            app.http.stop()


class TestWebSocketCleanup:
    """握手失败必须关掉 socket，否则重连循环一直漏 fd。"""

    def test_handshake_failure_closes_socket(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        import threading

        def serve_once() -> None:
            conn, _ = listener.accept()
            conn.recv(4096)
            # 故意回一个 200 而不是 101
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            conn.close()

        threading.Thread(target=serve_once, daemon=True).start()
        client = WSClient(f"ws://127.0.0.1:{port}/x")
        with pytest.raises(WebSocketError):
            client.connect(timeout=3)
        assert client.sock is None, "握手失败后 socket 没被释放"
        listener.close()

    def test_close_is_idempotent(self):
        client = WSClient("ws://127.0.0.1:1/x")
        client.close()
        client.close()
        client.close(graceful=False)


class TestIdleFlagPreserved:
    def test_started_keeps_idle_flag(self):
        item = QueueItem(keyword="随机", title="随机", path="/r.mp3", idle=True)
        assert item.with_playback_started().idle is True

    def test_real_order_is_not_idle(self):
        item = QueueItem(keyword="晴天", title="晴天", path="/a.mp3")
        assert item.with_playback_started().idle is False


class TestNoDeadAssertions:
    """防止 ``assert x or True`` 这种永远为真的假测试复活。"""

    def test_no_tautological_assertions(self):
        tests_dir = Path(__file__).resolve().parent
        offenders = []
        for path in tests_dir.glob("test_*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("assert ") and stripped.endswith("or True"):
                    offenders.append(f"{path.name}:{number}")
        assert not offenders, f"发现永远为真的断言：{offenders}"


class TestRootIsClean:
    def test_no_legacy_entry_points(self):
        """根目录不能再有 main.py / server.py 那套旧入口 —— 两个入口必然有人跑错。"""
        root = Path(__file__).resolve().parents[1]
        legacy = {"main.py", "server.py", "danmaku.py", "wsclient.py",
                  "qqmusic.py", "login_qr.py"}
        found = {p.name for p in root.glob("*.py")} & legacy
        assert not found, f"旧入口还在根目录：{sorted(found)}"

    def test_no_images_in_root(self):
        root = Path(__file__).resolve().parents[1]
        stray = [p.name for p in root.glob("*.png")] + [p.name for p in root.glob("*.ico")]
        assert not stray, f"图片还在根目录：{stray}"


class TestDocsExist:
    @pytest.mark.parametrize("name", ["README.md", "CHANGELOG.md", "docs/architecture.md"])
    def test_required_docs(self, name):
        root = Path(__file__).resolve().parents[1]
        assert (root / name).is_file(), f"缺文档：{name}"

    def test_changelog_is_numbered_and_append_only(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        import re
        numbers = [int(n) for n in re.findall(r"^## (\d+)\s", text, re.M)]
        assert numbers, "CHANGELOG 没有编号条目"
        assert numbers == sorted(numbers), "CHANGELOG 编号乱序"
        assert numbers[0] == 1, "编号应当从 1 开始"
        assert len(numbers) == len(set(numbers)), "编号重复"
        assert numbers == list(range(1, len(numbers) + 1)), "编号不连续"
