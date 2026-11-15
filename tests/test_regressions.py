"""回归守卫：把外审发现的每个坑钉成一条会失败的测试。

这里的每一条都对应一个**真实发生过的 bug**，不是凑覆盖率。
"""

from __future__ import annotations

import json
import os
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


class TestConcurrentAdvanceIsAtomic:
    """「比对 id → 切歌」必须是一步，否则两个页面同时上报就会各切一次。"""

    def _queue_with_three(self):
        queue = PlayQueue()
        first = QueueItem(keyword="a", title="a", path="/a.mp3")
        queue.push(first)
        for name in ("b", "c"):
            queue.push(QueueItem(keyword=name, title=name, path=f"/{name}.mp3"))
        return queue, first

    def test_advance_if_playing_rejects_stale_id(self):
        queue, first = self._queue_with_three()
        # 换成第二个播放项后，拿第一个的 id 来上报必须被拒
        queue.advance()
        switched, _ = queue.advance_if_playing(first.id)
        assert switched is False

    def test_advance_if_playing_accepts_current_id(self):
        queue, _ = self._queue_with_three()
        current = queue.playing.id
        switched, snapshot = queue.advance_if_playing(current)
        assert switched is True
        assert snapshot["playing"]["id"] != current

    def test_two_pages_racing_advance_only_once(self):
        """两个页面拿同一个 id 抢着上报，只能有一个生效。"""
        from diangeji.services.playback import PlaybackService
        queue, _ = self._queue_with_three()
        service = PlaybackService(queue=queue, registry=_NullRegistry())
        playing = queue.playing.id

        results = []
        import threading
        barrier = threading.Barrier(4)

        def report() -> None:
            barrier.wait()
            results.append(service.skip_if_finished(playing))

        threads = [threading.Thread(target=report) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert results.count(True) == 1, f"有 {results.count(True)} 个请求都切了歌"
        # 队列里还剩 1 首（3 首里切走 1 首），没被连切
        state = queue.snapshot()
        assert len(state["history"]) == 1


class _NullRegistry:
    """不刷新直链的假音源，避免测试联网。"""

    def refresh(self, track):
        return None


class TestPreviousDoesNotEatNewOrder:
    """previous() 锁外刷直链期间有人点歌，不能把新播放位覆盖掉。

    原实现重锁后无条件 ``self._playing = target``，会把并发 push() 刚顶上去
    的那首歌吞掉（advance() 有保护，previous() 没有）。
    """

    def test_previous_keeps_newer_push(self):
        queue = PlayQueue()
        queue.push(QueueItem(keyword="old", title="old", path="/old.mp3"))
        queue.advance()                       # old 进历史
        queue.push(QueueItem(keyword="mid", title="mid", path="/mid.mp3"))
        assert queue.playing.title == "mid"

        def steal(item):
            # 模拟锁外刷新期间，观众点了一首新歌抢到播放位
            queue.push(QueueItem(keyword="new", title="new", path="/new.mp3"))
            return item

        state = queue.previous(refresh=steal)
        # 新点的那首必须还在播，不能被历史项覆盖
        assert state["playing"]["title"] == "new", \
            f"previous() 吞掉了新点歌，当前是 {state['playing']['title']}"
        # 历史项退回历史，不丢
        assert any(item["title"] in ("old", "mid") for item in state["history"])


class TestAtomicWrite:
    def test_failure_leaves_no_tmp_file(self, tmp_path, monkeypatch):
        from diangeji import paths

        target = tmp_path / "x.txt"

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(paths.os, "replace", boom)
        with pytest.raises(OSError):
            paths.atomic_write(target, "data")
        leftover = list(tmp_path.glob("*.tmp"))
        assert not leftover, f"失败后留下了临时文件：{leftover}"

    def test_concurrent_writes_all_land_intact(self, tmp_path):
        """多个线程写同一个目标：临时文件名必须唯一，否则互相踩。"""
        from diangeji.paths import atomic_write
        import threading

        target = tmp_path / "c.txt"
        errors = []
        barrier = threading.Barrier(8)

        def write(index: int) -> None:
            barrier.wait()
            try:
                for _ in range(10):
                    atomic_write(target, f"value-{index}")
            except OSError as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert not errors, f"并发写报错：{errors[:3]}"
        assert target.read_text(encoding="utf-8").startswith("value-")
        assert not list(tmp_path.glob("*.tmp")), "留下了临时文件"


class TestDataDirIsStable:
    """pip 安装时数据目录不能跟着 cwd 跑 —— 换目录启动等于配置全丢。"""

    def test_installed_data_dir_is_not_cwd(self, monkeypatch):
        from diangeji import paths

        monkeypatch.setattr(paths, "_DEFAULT_BASE",
                            paths.Path("/nowhere/site-packages/diangeji"))
        monkeypatch.chdir(paths.Path.cwd())
        assert paths.data_dir() != paths.Path.cwd() / paths.DATA_DIR_NAME

    @pytest.mark.skipif(os.name != "nt", reason="目录名按平台不同")
    def test_installed_data_dir_has_no_double_nesting(self, monkeypatch):
        """回归：base_dir() 一旦返回「已带目录名」的路径，data_dir() 会拼成
        ``点歌姬数据\\点歌姬数据``。"""
        from diangeji import paths

        monkeypatch.setattr(paths, "_DEFAULT_BASE",
                            paths.Path("/nowhere/site-packages/diangeji"))
        target = paths.data_dir()
        assert target.name == paths.DATA_DIR_NAME
        assert target.parent.name != paths.DATA_DIR_NAME, \
            f"数据目录嵌套了两层：{target}"

    def test_source_layout_uses_project_root(self):
        from diangeji import paths
        # 本测试就是在 src/ 布局下跑的
        assert paths.base_dir() == paths.Path(__file__).resolve().parents[1]
        assert paths.data_dir().name == paths.DATA_DIR_NAME

    def test_data_dir_uses_override(self, tmp_path, monkeypatch):
        from diangeji import paths
        monkeypatch.setenv("DIANGE_DATA", str(tmp_path / "custom"))
        assert paths.data_dir() == (tmp_path / "custom").resolve()


class TestForbiddenIsDiagnosable:
    """老用户升级后挂 QQ 机器人会静默 403，提示必须写清楚怎么办。"""

    def test_403_body_mentions_flag(self, data_dir):
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        app.http.start()
        try:
            url = f"http://127.0.0.1:{app.http.port}/api/state"
            with pytest.raises(urllib.error.HTTPError) as info:
                urllib.request.urlopen(url, timeout=5)
            assert info.value.code == 403
            body = json.loads(info.value.read().decode("utf-8"))
            assert "allow-headless-api" in body.get("hint", "")
        finally:
            app.http.stop()


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
