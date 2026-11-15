"""Web 层：安全校验、模板转义、Range 解析、真实请求往返。"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from diangeji.core.library import LocalLibrary
from diangeji.core.queue import PlayQueue
from diangeji.services.playback import PlaybackService
from diangeji.sources.local_source import LocalSource
from diangeji.sources.registry import SourceRegistry
from diangeji.web import routes
from diangeji.web.routes import AppContext, _is_partial, _parse_range, build_server
from diangeji.web.security import is_local_request, parse_host, parse_origin
from diangeji.web.views import render_console, render_overlay, render_state_script


# ---------------------------------------------------------------- 安全校验

class TestParsing:
    @pytest.mark.parametrize("raw,expected", [
        ("127.0.0.1:8770", "127.0.0.1"),
        ("localhost:8770", "localhost"),
        ("127.0.0.1", "127.0.0.1"),
        ("[::1]:8770", "::1"),
        ("", ""),
        (None, ""),
    ])
    def test_parse_host(self, raw, expected):
        assert parse_host(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("http://127.0.0.1:8770", "127.0.0.1"),
        ("http://localhost:8770/x", "localhost"),
        ("https://evil.test", "evil.test"),
        ("", ""),
    ])
    def test_parse_origin(self, raw, expected):
        assert parse_origin(raw) == expected


class TestLocalRequest:
    def test_local_page_ok(self):
        assert is_local_request({"Host": "127.0.0.1:8770",
                                 "Origin": "http://127.0.0.1:8770"})

    def test_localhost_ok(self):
        assert is_local_request({"Host": "localhost:8770",
                                 "Referer": "http://localhost:8770/"})

    def test_foreign_origin_rejected(self):
        assert not is_local_request({"Host": "127.0.0.1:8770",
                                     "Origin": "http://evil.test"})

    def test_foreign_host_rejected(self):
        """DNS rebinding：Host 不是回环就拒。"""
        assert not is_local_request({"Host": "evil.test:8770",
                                     "Origin": "http://evil.test"})

    def test_dns_rebinding_host_with_local_origin(self):
        assert not is_local_request({"Host": "attacker.example",
                                     "Origin": "http://127.0.0.1:8770"})

    def test_no_headers_rejected_by_default(self):
        """原实现「两个头都没有就放行」，等于对本机任意进程全开。"""
        assert not is_local_request({"Host": "127.0.0.1:8770"})

    def test_headless_allowed_on_demand(self):
        assert is_local_request({"Host": "127.0.0.1:8770"}, allow_headless=True)

    def test_missing_host_rejected(self):
        assert not is_local_request({"Origin": "http://127.0.0.1:8770"})


# ---------------------------------------------------------------- 模板

class TestViews:
    def test_script_escaping(self):
        payload = render_state_script({"title": "</script><script>alert(1)</script>"})
        assert "</script>" not in payload
        assert "<\\/script>" in payload
        assert json.loads(payload)["title"].startswith("</script>")

    def test_line_separators_escaped(self):
        payload = render_state_script({"x": "a\u2028b\u2029c"})
        assert "\u2028" not in payload and "\u2029" not in payload

    def test_comment_open_escaped(self):
        assert "<!--" not in render_state_script({"x": "<!--"})

    def test_render_console_substitutes(self, tmp_path):
        (tmp_path / "index.html").write_text(
            "<p>__LIBCOUNT__ / __VERSION__</p><script>__STATE__</script>", encoding="utf-8")
        html = render_console(tmp_path, {"a": 1}, 42, "9.9.9")
        assert "42" in html and "9.9.9" in html
        assert '"a": 1' in html and "__STATE__" not in html

    def test_render_overlay(self, tmp_path):
        (tmp_path / "overlay.html").write_text("v__VERSION__", encoding="utf-8")
        assert render_overlay(tmp_path, "1.2.3") == "v1.2.3"


# ---------------------------------------------------------------- Range

class TestRange:
    def test_no_header(self):
        assert _parse_range(None, 1000) == (0, 999)

    def test_open_ended(self):
        assert _parse_range("bytes=100-", 1000) == (100, 999)

    def test_closed(self):
        assert _parse_range("bytes=100-199", 1000) == (100, 199)

    def test_suffix(self):
        """原实现把 ``bytes=-500`` 当成 0-500，播到一半会跳。"""
        assert _parse_range("bytes=-500", 1000) == (500, 999)

    def test_suffix_larger_than_file(self):
        assert _parse_range("bytes=-5000", 1000) == (0, 999)

    def test_start_beyond_size(self):
        assert _parse_range("bytes=2000-", 1000) is None

    def test_empty_file(self):
        assert _parse_range("bytes=0-", 0) is None

    def test_end_clamped(self):
        assert _parse_range("bytes=0-99999", 1000) == (0, 999)

    def test_multi_range_falls_back(self):
        assert _parse_range("bytes=0-10,20-30", 1000) == (0, 999)

    def test_garbage(self):
        assert _parse_range("items=0-10", 1000) == (0, 999)

    def test_malformed_bytes_spec_is_ignored(self):
        """``bytes=abc-def`` 按 RFC 应当忽略这个头，回整份文件。

        关键是不能回 ``None`` —— 那是 416 的意思，会让播放器直接放弃。
        """
        assert _parse_range("bytes=abc-def", 1000) == (0, 999)

    def test_double_dash(self):
        assert _parse_range("bytes=1-2-3", 1000) == (0, 999)

    def test_bare_equals(self):
        assert _parse_range("bytes=", 1000) == (0, 999)


class TestPartialResponse:
    """畸形 Range 不该发出「声称部分内容、实际整份文件」的 206。"""

    def test_no_range_is_not_partial(self):
        assert _is_partial(None, (0, 999), 1000) is False

    def test_malformed_range_is_not_partial(self):
        assert _is_partial("bytes=abc-def", (0, 999), 1000) is False

    def test_real_range_is_partial(self):
        assert _is_partial("bytes=0-99", (0, 99), 1000) is True

    def test_full_span_range_is_not_partial(self):
        """``bytes=0-`` 覆盖整个文件，发 206 没有意义。"""
        assert _is_partial("bytes=0-", (0, 999), 1000) is False


# ---------------------------------------------------------------- 真实往返

@pytest.fixture()
def live_server(tmp_path, audio_tree):
    """起一个真的 HTTP 服务（端口 0 自动分配），测完关掉。"""
    library = LocalLibrary()
    library.scan([str(audio_tree)])
    registry = SourceRegistry([LocalSource(library)])
    playback = PlaybackService(queue=PlayQueue(idle=False), registry=registry)

    templates = tmp_path / "tpl"
    templates.mkdir()
    (templates / "index.html").write_text(
        "<html>lib=__LIBCOUNT__ v=__VERSION__<script>__STATE__</script></html>",
        encoding="utf-8")
    (templates / "overlay.html").write_text("overlay v__VERSION__", encoding="utf-8")
    images = tmp_path / "img"
    (images / "btn").mkdir(parents=True)
    (images / "bg.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
    (images / "btn" / "btn_1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 16)

    ctx = AppContext(playback=playback, library=library, template_dir=templates,
                     image_dir=images, button_dir=images / "btn", version="0.0.1",
                     allow_headless=True)
    server = build_server(ctx, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, playback
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def get(url, range_header=None):
    request = urllib.request.Request(url)
    if range_header:
        request.add_header("Range", range_header)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, dict(response.headers), response.read()


def post(url, payload=None):
    data = json.dumps(payload or {}).encode()
    request = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read() or b"{}")


class TestLiveServer:
    def test_console_page(self, live_server):
        base, playback = live_server
        status, _, body = get(base + "/")
        assert status == 200
        text = body.decode()
        assert "lib=" in text and "0.0.1" in text

    def test_state_api(self, live_server):
        base, playback = live_server
        status, _, body = get(base + "/api/state")
        assert status == 200
        payload = json.loads(body)
        assert "playing" in payload and "queue" in payload

    def test_order_then_state(self, live_server):
        base, playback = live_server
        post(base + "/api/order", {"text": "点歌 晴天", "user": "小明"})
        _, _, body = get(base + "/api/state")
        payload = json.loads(body)
        assert payload["playing"]["title"] == "晴天"
        assert payload["playing"]["user"] == "小明"

    def test_ended_only_advances_matching_id(self, live_server):
        """核心回归：两个页面同时上报 ended，只该切一首。"""
        base, playback = live_server
        post(base + "/api/order", {"text": "点歌 晴天"})
        post(base + "/api/order", {"text": "点歌 晴天娃娃"})
        _, _, body = get(base + "/api/state")
        current = json.loads(body)["playing"]
        assert current["title"] == "晴天"

        # 主播台先报
        _, first = post(base + "/api/ended", {"id": current["id"]})
        assert first["advanced"] is True
        assert first["state"]["playing"]["title"] == "晴天娃娃"

        # OBS 层迟到的那次：ID 已经过期，必须被丢弃
        _, second = post(base + "/api/ended", {"id": current["id"]})
        assert second["advanced"] is False
        assert second["state"]["playing"]["title"] == "晴天娃娃"

    def test_ended_unknown_id_ignored(self, live_server):
        base, playback = live_server
        post(base + "/api/order", {"text": "点歌 晴天"})
        _, body = post(base + "/api/ended", {"id": "deadbeef"})
        assert body["advanced"] is False

    def test_media_full(self, live_server, audio_tree):
        base, playback = live_server
        status, headers, body = get(base + "/media/%E6%99%B4%E5%A4%A9.mp3")
        assert status == 200
        assert headers["Accept-Ranges"] == "bytes"
        assert len(body) == 301 * 1024

    def test_media_range(self, live_server):
        base, playback = live_server
        status, headers, body = get(base + "/media/%E6%99%B4%E5%A4%A9.mp3",
                                    "bytes=0-99")
        assert status == 206
        assert headers["Content-Range"] == f"bytes 0-99/{301 * 1024}"
        assert len(body) == 100

    def test_media_suffix_range(self, live_server):
        base, playback = live_server
        size = 301 * 1024
        status, headers, body = get(base + "/media/%E6%99%B4%E5%A4%A9.mp3",
                                    "bytes=-512")
        assert status == 206
        assert headers["Content-Range"] == f"bytes {size - 512}-{size - 1}/{size}"
        assert len(body) == 512

    def test_media_empty_path_404(self, live_server):
        """GET /media/ 不能把曲库第一首吐出来。"""
        base, playback = live_server
        with pytest.raises(urllib.error.HTTPError) as info:
            get(base + "/media/")
        assert info.value.code == 404

    def test_media_substring_404(self, live_server):
        base, playback = live_server
        with pytest.raises(urllib.error.HTTPError) as info:
            get(base + "/media/e.mp3")
        assert info.value.code == 404

    def test_media_missing_404(self, live_server):
        base, playback = live_server
        with pytest.raises(urllib.error.HTTPError) as info:
            get(base + "/media/nope.mp3")
        assert info.value.code == 404

    def test_asset_png(self, live_server):
        base, playback = live_server
        status, headers, body = get(base + "/asset/bg.png")
        assert status == 200 and headers["Content-Type"] == "image/png"

    def test_asset_button(self, live_server):
        base, playback = live_server
        status, _, _ = get(base + "/asset/btn_1.png")
        assert status == 200

    def test_asset_traversal_blocked(self, live_server):
        base, playback = live_server
        with pytest.raises(urllib.error.HTTPError) as info:
            get(base + "/asset/..%2F..%2Fconfig.json")
        assert info.value.code == 404

    def test_volume(self, live_server):
        base, playback = live_server
        _, body = post(base + "/api/volume", {"value": 66})
        assert body["state"]["volume"] == 66

    def test_pause_toggle(self, live_server):
        base, playback = live_server
        post(base + "/api/order", {"text": "点歌 晴天"})
        _, body = post(base + "/api/pause")
        assert body["state"]["paused"] is True

    def test_unknown_api_404(self, live_server):
        base, playback = live_server
        with pytest.raises(urllib.error.HTTPError) as info:
            post(base + "/api/nope")
        assert info.value.code == 404

    def test_overlay_page(self, live_server):
        base, playback = live_server
        status, _, body = get(base + "/overlay")
        assert status == 200 and b"overlay v0.0.1" in body


class TestForbidden:
    """关掉 allow_headless 后，没有 Origin/Referer 的请求必须被拒。"""

    def test_api_forbidden_without_origin(self, tmp_path):
        library = LocalLibrary()
        playback = PlaybackService(queue=PlayQueue(idle=False),
                                   registry=SourceRegistry([LocalSource(library)]))
        templates = tmp_path / "t"
        templates.mkdir()
        (templates / "index.html").write_text("x", encoding="utf-8")
        (templates / "overlay.html").write_text("y", encoding="utf-8")
        ctx = AppContext(playback=playback, library=library, template_dir=templates,
                         image_dir=tmp_path, button_dir=tmp_path, allow_headless=False)
        server = build_server(ctx, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with pytest.raises(urllib.error.HTTPError) as info:
                get(base + "/api/state")
            assert info.value.code == 403
            # 页面本身仍可访问（不然连界面都打不开）
            assert get(base + "/")[0] == 200
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_api_allowed_with_local_origin(self, tmp_path):
        library = LocalLibrary()
        playback = PlaybackService(queue=PlayQueue(idle=False),
                                   registry=SourceRegistry([LocalSource(library)]))
        templates = tmp_path / "t"
        templates.mkdir()
        (templates / "index.html").write_text("x", encoding="utf-8")
        (templates / "overlay.html").write_text("y", encoding="utf-8")
        ctx = AppContext(playback=playback, library=library, template_dir=templates,
                         image_dir=tmp_path, button_dir=tmp_path)
        server = build_server(ctx, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/api/state")
            request.add_header("Origin", f"http://127.0.0.1:{port}")
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 200
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


# ---------------------------------------------------------------- 审计发现的回归

def _raw_get(port, path, host):
    """发一个指定 Host 头的原始请求，拿到 (status, body)。

    不能用 urllib —— 它不让改 Host。
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers={"Host": host})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


@pytest.fixture()
def quiet_server(tmp_path, audio_tree):
    """和 live_server 一样，但不放行无头请求 —— 用来测安全边界。"""
    library = LocalLibrary()
    library.scan([str(audio_tree)])
    registry = SourceRegistry([LocalSource(library)])
    playback = PlaybackService(queue=PlayQueue(idle=False), registry=registry)
    templates = tmp_path / "tpl2"
    templates.mkdir()
    (templates / "index.html").write_text("<html>__STATE__</html>", encoding="utf-8")
    (templates / "overlay.html").write_text("overlay", encoding="utf-8")
    images = tmp_path / "img2"
    images.mkdir()
    (images / "bg.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 16)
    ctx = AppContext(playback=playback, library=library, template_dir=templates,
                     image_dir=images, button_dir=images, version="0.0.1",
                     allow_headless=False)
    server = build_server(ctx, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], playback, templates
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class TestHostValidation:
    """DNS rebinding：Host 不是回环，连页面和图片都不许读。

    只挡写入是没用的 —— 主播台 HTML 里带着完整队列和观众昵称。
    """

    def test_forged_host_cannot_read_console(self, quiet_server):
        port, _, _ = quiet_server
        status, _ = _raw_get(port, "/", f"evil.com:{port}")
        assert status == 403

    def test_forged_host_cannot_read_overlay(self, quiet_server):
        port, _, _ = quiet_server
        status, _ = _raw_get(port, "/overlay", f"evil.com:{port}")
        assert status == 403

    def test_forged_host_cannot_read_asset(self, quiet_server):
        port, _, _ = quiet_server
        status, _ = _raw_get(port, "/asset/bg.png", f"evil.com:{port}")
        assert status == 403

    def test_loopback_host_still_works(self, quiet_server):
        port, _, _ = quiet_server
        status, body = _raw_get(port, "/", f"127.0.0.1:{port}")
        assert status == 200
        assert b"<html>" in body

    def test_localhost_alias_works(self, quiet_server):
        port, _, _ = quiet_server
        status, _ = _raw_get(port, "/", f"localhost:{port}")
        assert status == 200

    def test_is_host_allowed_unit(self):
        from diangeji.web.security import is_host_allowed
        assert is_host_allowed({"Host": "127.0.0.1:8770"}) is True
        assert is_host_allowed({"Host": "localhost"}) is True
        assert is_host_allowed({"Host": "[::1]:8770"}) is True
        assert is_host_allowed({"Host": "evil.com:8770"}) is False

    def test_ipv6_loopback_is_in_the_single_source_of_truth(self):
        """``::1`` 必须在 ``constants.LOCAL_HOSTS`` 里，不能只靠 web 层打补丁。

        Windows 上 ``localhost`` 常先解析到 ``::1``；早先只有 ``web.security``
        自己并了一份 ``{"::1", "[::1]"}``，于是「哪些 host 算本机」有两个真相：
        别的模块按 ``constants`` 判断就会误拒。
        """
        from diangeji.constants import LOCAL_HOSTS
        assert "::1" in LOCAL_HOSTS
        assert "[::1]" in LOCAL_HOSTS
        assert "127.0.0.1" in LOCAL_HOSTS
        assert "localhost" in LOCAL_HOSTS
        # 而且这些地址真的能过校验（不是只在集合里躺着）
        from diangeji.web.security import is_host_allowed
        for host in ("127.0.0.1:8770", "localhost:8770", "[::1]:8770"):
            assert is_host_allowed({"Host": host}) is True, host
        assert is_host_allowed({"Host": ""}) is False
        assert is_host_allowed({}) is False


class TestServerInternals:
    """这些原来零测试 —— 两个 bug 就住在这里。"""

    def test_port_available_sees_occupied_port(self):
        """被独占占用的端口必须报告 False。

        原来的探针用 SO_REUSEADDR，Windows 上允许 bind 已占端口，
        于是永远返回 True —— 它唯一要回答的问题答错了。
        """
        from diangeji.web.server import HttpService
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            port = blocker.getsockname()[1]
            assert HttpService.port_available(port) is False

    def test_port_available_reports_free_port(self):
        from diangeji.web.server import HttpService
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        assert HttpService.port_available(free) is True

    def test_wait_ready_true_for_live_server(self, quiet_server):
        """原来判据写反了，`running` 一真就立刻返回 True，等于没验证。"""
        port, _, _ = quiet_server
        from diangeji.web.server import HttpService
        service = HttpService.__new__(HttpService)
        service.host = "127.0.0.1"
        service.port = port
        service.error = ""
        service._thread = threading.current_thread()   # 活着的线程即可
        assert service.wait_ready(timeout=3.0) is True

    def test_wait_ready_false_for_dead_port(self):
        from diangeji.web.server import HttpService
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            dead = probe.getsockname()[1]
        service = HttpService.__new__(HttpService)
        service.host = "127.0.0.1"
        service.port = dead
        service.error = ""
        service._thread = threading.current_thread()
        assert service.wait_ready(timeout=0.6) is False


class TestErrorDisclosure:
    """500 不能把服务端细节（绝对路径等）交给浏览器。"""

    def test_missing_template_does_not_leak_path(self, tmp_path, audio_tree):
        library = LocalLibrary()
        library.scan([str(audio_tree)])
        playback = PlaybackService(queue=PlayQueue(idle=False),
                                   registry=SourceRegistry([LocalSource(library)]))
        empty = tmp_path / "no_templates"
        empty.mkdir()
        ctx = AppContext(playback=playback, library=library, template_dir=empty,
                         image_dir=tmp_path, button_dir=tmp_path, allow_headless=True)
        server = build_server(ctx, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = _raw_get(server.server_address[1], "/",
                                    f"127.0.0.1:{server.server_address[1]}")
            text = body.decode("utf-8", "replace")
            assert status == 500
            assert str(tmp_path) not in text, "500 响应体泄露了绝对路径"
            assert "index.html" not in text or "模板" in text
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_render_missing_template_raises_domain_error(self, tmp_path):
        from diangeji.core.errors import LibraryUnavailableError
        with pytest.raises(LibraryUnavailableError):
            render_console(tmp_path, {}, 0, "1.0")


class TestDomainErrors:
    """领域异常真的被抛、真的被区分成 400 / 500。"""

    def test_empty_keyword_raises_domain_error(self):
        from diangeji.core.errors import EmptyKeywordError
        playback = PlaybackService(queue=PlayQueue(idle=False),
                                   registry=SourceRegistry([]))
        with pytest.raises(EmptyKeywordError):
            playback.request("观众", "   ")

    def test_empty_keyword_is_still_valueerror(self):
        """老代码可能只 except ValueError，不能把它们弄崩。"""
        from diangeji.core.errors import EmptyKeywordError
        assert issubclass(EmptyKeywordError, ValueError)

    def test_status_codes_are_distinguished(self):
        from diangeji.core.errors import (DianGeJiError, EmptyKeywordError,
                                          LibraryUnavailableError)
        assert EmptyKeywordError.http_status == 400
        assert LibraryUnavailableError.http_status == 500
        assert DianGeJiError.http_status == 400

    @pytest.mark.parametrize("path,status", [
        ("/api/order", 400),
    ])
    def test_empty_order_returns_400(self, quiet_server, path, status):
        port, _, _ = quiet_server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            body = json.dumps({"text": "  ", "user": "x"}).encode()
            conn.request("POST", path, body=body,
                         headers={"Host": f"127.0.0.1:{port}",
                                  "Content-Type": "application/json",
                                  "Origin": f"http://127.0.0.1:{port}"})
            response = conn.getresponse()
            assert response.status == status
            assert json.loads(response.read())["ok"] is False
        finally:
            conn.close()


class TestBodyDrain:
    """超大 body 不排空，会污染 keep-alive 连接上的下一个请求。"""

    def test_oversized_body_does_not_poison_connection(self, quiet_server):
        """超大 body 不排空时，残留字节会被当成下一条请求的请求行。

        原实现的实测症状是服务端回
        ``501 Unsupported method ('{"text":"x"}GET')``。
        修复后服务端应当**关掉连接**，而不是带着脏 buffer 继续用。

        用原始 socket 而不是 ``http.client``：后者在写超限 body 时会先收到
        ``ConnectionAbortedError``（服务端已经关了），那正是预期结果，
        但看不清「到底关干净没有」。
        """
        port, _, _ = quiet_server
        huge = b"x" * (1000 * 1024)          # 超过 MAX_BODY_BYTES(1_000_000)
        payload = (f"POST /api/order HTTP/1.1\r\n"
                   f"Host: 127.0.0.1:{port}\r\n"
                   f"Content-Type: application/json\r\n"
                   f"Origin: http://127.0.0.1:{port}\r\n"
                   f"Content-Length: {len(huge)}\r\n\r\n").encode() + huge
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            try:
                sock.sendall(payload)
            except OSError:
                pass                          # 服务端提前关了，正常
            sock.settimeout(5)
            received = b""
            try:
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    received += chunk
            except (OSError, socket.timeout):
                pass
        text = received.decode("latin-1", "replace")
        # 关键断言：绝不能出现「把 body 当请求行」的 501
        assert "501" not in text, f"连接被污染了：{text[:200]}"
        assert "Unsupported method" not in text

    def test_range_malformed_returns_200_not_206(self, live_server):
        """★ 畸形 Range 不能回「声称部分内容、实际整份」的 206。"""
        base, _ = live_server
        status, headers, body = get(base + "/media/" + urllib.parse.quote("晴天.mp3"), "bytes=abc-def")
        assert status == 200, "畸形 Range 应当忽略，返回 200 整份"
        assert "Content-Range" not in headers

    def test_valid_range_still_206(self, live_server):
        base, _ = live_server
        status, headers, _ = get(base + "/media/" + urllib.parse.quote("晴天.mp3"), "bytes=0-99")
        assert status == 206
        assert headers["Content-Range"].startswith("bytes 0-99/")
