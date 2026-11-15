"""装配层与模板契约：组合根能起来、模板占位符对得上、前端字段名不写错。

这里不测 Tk 界面（需要显示器），只测「不依赖界面也能坏掉」的部分。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from diangeji import paths
from diangeji.app import Application, build_application, _default_idle_pool
from diangeji.config import Config
from diangeji.core.queue import PlayQueue
from diangeji.services.playback import PlaybackService
from diangeji.sources.local_source import LocalSource
from diangeji.sources.registry import SourceRegistry


# ---------------------------------------------------------------- 组合根

class TestBuildApplication:
    def test_builds_without_network(self, data_dir):
        """装配不该联网：cookie 没登录时 QQ 音源直接跳过。"""
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        assert isinstance(app, Application)
        assert app.playback is not None
        assert app.http.port == 0

    def test_config_roundtrip_through_build(self, data_dir):
        Config(room_id="99999", volume=44).save(data_dir / "config.json")
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        assert app.config.room_id == "99999"
        assert app.playback.snapshot()["volume"] == 44

    def test_qq_source_disabled_without_cookie(self, data_dir):
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        assert app.qq_source.enabled() is False

    def test_idle_pool_override(self, data_dir):
        (data_dir / "idle_pool.txt").write_text("# 注释\n自定义歌 A\n自定义歌 B\n",
                                                encoding="utf-8")
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        assert "自定义歌 A" in app.playback._idle_pool
        assert all(not line.startswith("#") for line in app.playback._idle_pool)

    def test_default_idle_pool_non_empty(self):
        assert len(_default_idle_pool()) > 5

    def test_urls(self, data_dir):
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        assert app.console_url().endswith("?mute=1")
        assert not app.console_url(mute=False).endswith("?mute=1")
        assert app.overlay_url().endswith("/overlay")

    def test_danmaku_state_when_idle(self, data_dir):
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        state = app.danmaku_state()
        assert state["connected"] is False
        assert state["room"] is None

    def test_shutdown_is_safe_before_start(self, data_dir):
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        app.shutdown()   # 没启动也要能安全收尾
        app.shutdown()

    def test_allow_headless_flag(self, data_dir):
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        assert app.http.ctx.allow_headless is False
        app.allow_headless_api(True)
        assert app.http.ctx.allow_headless is True


class TestOrderWorker:
    """弹幕收包线程只投递，解析在单 worker 里顺序跑。

    原实现 ``_handle_order`` 直接在收包线程里做「搜歌 + 取直链」（最坏 24 秒），
    而弹幕心跳窗口只有 25 秒 —— 一次慢查询就能把长连接拖死。
    """

    def test_order_is_queued_not_executed_inline(self, data_dir, audio_tree):
        import time
        from diangeji.live.commands import Order
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        try:
            # 空曲库里搜不到歌，先扫出曲库才有歌可点
            app.library_service.scan([str(audio_tree)])
            title = app.library.search("晴天")[0]["title"]
            app._handle_order("观众", Order(keyword=title, raw_text="点歌 " + title))
            # 立刻返回（不阻塞收包线程），解析在后台进行
            deadline = time.time() + 5
            while time.time() < deadline:
                if app.playback.snapshot()["playing"] is not None:
                    break
                time.sleep(0.05)
            playing = app.playback.snapshot()["playing"]
            assert playing is not None, "worker 没有把点歌入队"
            assert playing["user"] == "观众"
        finally:
            app.shutdown()

    def test_worker_keeps_order(self, data_dir, audio_tree):
        import time
        from diangeji.live.commands import Order
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        try:
            app.library_service.scan([str(audio_tree)])
            title = app.library.search("晴天")[0]["title"]
            for index in range(5):
                app._handle_order(f"观众{index}",
                                  Order(keyword=title, raw_text="点歌 " + title))
            deadline = time.time() + 8
            while time.time() < deadline:
                state = app.playback.snapshot()
                if len(state["queue"]) + (1 if state["playing"] else 0) >= 5:
                    break
                time.sleep(0.05)
            state = app.playback.snapshot()
            pending = ([state["playing"]] if state["playing"] else []) + state["queue"]
            users = [item["user"] for item in pending]
            assert users[:5] == [f"观众{i}" for i in range(5)], f"顺序乱了：{users}"
        finally:
            app.shutdown()

    def test_worker_handles_resolve_failure(self, data_dir):
        import time
        from diangeji.live.commands import Order
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        try:
            app._handle_order("观众", Order(keyword="完全不存在的一首歌", raw_text="点歌 完全不存在的一首歌"))
            deadline = time.time() + 5
            while time.time() < deadline:
                if app.playback.snapshot()["queue"]:
                    break
                time.sleep(0.05)
            # 解析失败也要入队，不能凭空消失
            assert app.playback.snapshot()["queue"], "解析失败的歌没进队列"
        finally:
            app.shutdown()

    def test_shutdown_stops_worker(self, data_dir):
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        worker = app._order_worker_thread
        assert worker is not None and worker.is_alive()
        app.shutdown()
        assert not worker.is_alive(), "worker 没有在 shutdown 时退出"

    def test_shutdown_twice_is_safe(self, data_dir):
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        app.shutdown()
        app.shutdown()


class TestHttpLifecycle:
    def test_start_and_stop(self, data_dir):
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        app.allow_headless_api(True)      # 脚本调用没有 Origin，测试里显式放开
        assert app.http.start() is True
        assert app.http.running is True
        port = app.http.port
        assert port > 0

        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state",
                                    timeout=5) as response:
            payload = json.loads(response.read())
        assert "playing" in payload

        app.http.stop()
        assert app.http.running is False

    def test_double_start_is_idempotent(self, data_dir):
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        assert app.http.start() is True
        first_port = app.http.port
        assert app.http.start() is True
        assert app.http.port == first_port
        app.http.stop()

    def test_port_conflict_reports_error(self, data_dir):
        blocker = build_application(data_directory=data_dir, port=0, with_logging=False)
        blocker.http.start()
        port = blocker.http.port
        other = build_application(data_directory=data_dir, port=port, with_logging=False)
        try:
            assert other.http.start() is False
            assert "占用" in other.http.error
        finally:
            blocker.http.stop()
            other.http.stop()

    def test_stop_before_start(self, data_dir):
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        app.http.stop()
        assert app.http.running is False

    def test_stop_before_start(self, data_dir):
        app = build_application(data_directory=data_dir, port=0, with_logging=False)
        app.http.stop()
        assert app.http.running is False


# ---------------------------------------------------------------- 模板契约

TEMPLATE_DIR = paths.template_dir()


def template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


class TestTemplates:
    def test_console_placeholders_present(self):
        html = template("index.html")
        for placeholder in ("__STATE__", "__LIBCOUNT__", "__VERSION__"):
            assert placeholder in html, f"模板少了 {placeholder}"

    def test_overlay_placeholder_present(self):
        assert "__VERSION__" in template("overlay.html")

    def test_console_has_no_stray_placeholders(self):
        """渲染后再检查：除已知的三个，不该有别的双下划线占位符漏出来。"""
        html = template("index.html")
        found = set(re.findall(r"__[A-Z][A-Z0-9_]*__", html))
        assert found <= {"__STATE__", "__LIBCOUNT__", "__VERSION__"}

    def test_no_hardcoded_asset_paths(self):
        """图片已经搬进 web/static/img，模板不该再引用根目录文件。"""
        for name in ("index.html", "overlay.html"):
            html = template(name)
            assert 'src="bg.png"' not in html
            assert 'src="hero.png"' not in html
            assert 'src="icon.ico"' not in html

    def test_console_uses_asset_route(self):
        assert "/asset/hero_web.png" in template("index.html")

    def test_both_pages_report_ended_with_id(self):
        """回归守卫：两个页面都必须带 id 上报，否则双跳歌会回来。"""
        for name in ("index.html", "overlay.html"):
            html = template(name)
            assert "/api/ended" in html, f"{name} 没走 /api/ended"
            assert "JSON.stringify({id:" in html.replace(" ", ""), \
                f"{name} 上报 ended 时没带 id"

    def test_ended_handler_does_not_call_skip(self):
        """onended 里只能走 /api/ended；/api/skip 留给「下一首」按钮。"""
        for name in ("index.html", "overlay.html"):
            html = template(name)
            # 找到 onended 回调范围，里面不该出现 /api/skip
            match = re.search(r"addEventListener\(\s*['\"]ended['\"](.*?)\}\);",
                              html, re.S)
            assert match, f"{name} 没有 ended 监听"
            assert "/api/skip" not in match.group(1), \
                f"{name} 的 onended 还在直接调 /api/skip"

    def test_ended_is_deduplicated_client_side(self):
        """同一页面内也要只报一次（endedSent 标志）。"""
        for name in ("index.html", "overlay.html"):
            assert "endedSent" in template(name)

    def test_console_polls_state(self):
        assert "/api/state" in template("index.html")
        assert re.search(r"setInterval\(\s*refresh\s*,\s*\d+\s*\)", template("index.html"))

    def test_overlay_polls_state(self):
        assert re.search(r"setInterval\(\s*tick\s*,\s*\d+\s*\)", template("overlay.html"))

    def test_console_is_muted(self):
        """主播台静音，声音只从 OBS 层出。"""
        assert "MUTE" in template("index.html")
        assert "au.muted = MUTE" in template("index.html")

    def test_overlay_not_muted(self):
        html = template("overlay.html")
        assert "au.muted" not in html

    def test_overlay_fixed_size(self):
        """覆盖层必须是固定尺寸，不能因为歌名长短而跳动。"""
        html = template("overlay.html")
        assert "width:600px" in html and "height:92px" in html

    def test_overlay_marquee_only_when_overflow(self):
        assert "scrollWidth" in template("overlay.html")

    def test_templates_escape_dynamic_text(self):
        """插入 DOM 的动态文本必须过 esc()。"""
        for name in ("index.html", "overlay.html"):
            html = template(name)
            assert "function esc(" in html
            assert "${esc(" in html

    def test_api_paths_used_are_routed(self):
        """前端调的每个 /api/ 路径，后端都得有处理分支。"""
        from diangeji.web.routes import RequestHandler
        routed = set(re.findall(r'"(/api/[a-z]+)"',
                                Path(RequestHandler.__module__.replace(".", "/") + ".py")
                                .read_text(encoding="utf-8")
                                if Path(RequestHandler.__module__.replace(".", "/") + ".py").is_file()
                                else ""))
        # 直接从源码文件读，路径推导不靠谱时用模块文件
        source = Path(__import__("diangeji.web.routes", fromlist=["x"]).__file__).read_text(
            encoding="utf-8")
        routed = set(re.findall(r'"(/api/[a-z]+)"', source))
        used = set()
        for name in ("index.html", "overlay.html"):
            used |= set(re.findall(r"['\"]?(/api/[a-z]+)", template(name)))
        missing = used - routed
        assert not missing, f"前端调了后端没有的接口：{sorted(missing)}"

    def test_state_fields_used_by_frontend_exist(self, data_dir):
        """前端读的字段，snapshot() 里必须真有。"""
        queue = PlayQueue(idle=False)
        snapshot = queue.snapshot()
        for field in ("playing", "queue", "history", "paused", "volume", "idle", "log"):
            assert field in snapshot, f"snapshot 少了 {field}"

    def test_playing_fields_used_by_frontend_exist(self):
        from diangeji.core.models import QueueItem
        item = QueueItem(keyword="x", title="x", user="u", path="/a.mp3")
        data = item.to_dict()
        for field in ("id", "title", "user", "path", "source"):
            assert field in data, f"QueueItem.to_dict 少了 {field}"


# ---------------------------------------------------------------- 打包配置

class TestPackaging:
    def test_pyproject_exists_and_parses(self):
        import tomllib
        root = Path(__file__).resolve().parents[1]
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        assert data["project"]["name"] == "diangeji"
        assert data["tool"]["setuptools"]["package-dir"][""] == "src"

    def test_package_data_covers_templates_and_images(self):
        import tomllib
        root = Path(__file__).resolve().parents[1]
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        package_data = data["tool"]["setuptools"]["package-data"]["diangeji"]
        joined = " ".join(package_data)
        assert "templates" in joined
        assert "img" in joined

    def test_entry_points_point_at_main(self):
        import tomllib
        root = Path(__file__).resolve().parents[1]
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = data["project"]["scripts"]
        assert scripts["diangeji"] == "diangeji.__main__:main"

    def test_run_script_exists(self):
        root = Path(__file__).resolve().parents[1]
        assert (root / "run.py").is_file()

    def test_no_flat_modules_left_in_root(self):
        """根目录不该再有平铺的 .py 业务模块（run.py 是启动器，允许）。"""
        root = Path(__file__).resolve().parents[1]
        allowed = {"run.py"}
        stray = {p.name for p in root.glob("*.py")} - allowed
        assert not stray, f"根目录还有平铺模块：{sorted(stray)}"


# ---------------------------------------------------------------- CLI

class TestCli:
    def test_parse_defaults(self):
        from diangeji.__main__ import parse_args
        args = parse_args([])
        assert args.port == 8770
        assert args.headless is False
        assert args.allow_headless_api is False

    def test_parse_room_and_port(self):
        from diangeji.__main__ import parse_args
        args = parse_args(["--room", "12345", "--port", "9000", "--headless"])
        assert args.room == "12345"
        assert args.port == 9000
        assert args.headless is True

    def test_version_flag(self, capsys):
        from diangeji.__main__ import parse_args
        with pytest.raises(SystemExit):
            parse_args(["--version"])

    def test_unknown_flag_rejected(self):
        from diangeji.__main__ import parse_args
        with pytest.raises(SystemExit):
            parse_args(["--nope"])


class TestLogFileFollowsDataDir:
    """``--data-dir`` 必须同时决定日志位置。

    原来 ``build_application()` 调的是无参 ``setup_logging()``，日志路径
    永远取默认的 ``data_dir()`` —— 传了 ``--data-dir D:\\foo`` 之后，
    配置和曲库进了新目录，日志却还写在程序旁边，按文档去找必然扑空。
    """

    def test_log_written_into_given_data_dir(self, tmp_path):
        from diangeji.app import build_application
        target = tmp_path / "custom"
        build_application(data_directory=target, port=0, with_logging=True)
        log_file = target / "logs" / "diangeji.log"
        assert log_file.is_file(), f"日志没写进指定目录：{log_file}"
        # 组装阶段不保证已有内容（真正写日志发生在 start 之后），
        # 文件被创建即证明 handler 指对了地方。
        assert log_file.parent == target / "logs"

    def test_default_log_path_untouched(self, tmp_path):
        """指定目录时不能顺手在默认目录里也建一份。"""
        from diangeji import paths
        from diangeji.app import build_application
        default_log = paths.data_dir() / "logs" / "diangeji.log"
        before = default_log.stat().st_mtime if default_log.exists() else None
        build_application(data_directory=tmp_path / "other", port=0,
                          with_logging=True)
        after = default_log.stat().st_mtime if default_log.exists() else None
        assert before == after, "指定 --data-dir 时默认目录的日志被动了"


class TestDataDirPriority:
    """``--data-dir`` > ``DIANGE_DATA`` > 默认。文档就是这么写的。"""

    def test_cli_overrides_env(self, tmp_path, monkeypatch):
        from diangeji.app import build_application
        monkeypatch.setenv("DIANGE_DATA", str(tmp_path / "fromenv"))
        cli = tmp_path / "fromcli"
        app = build_application(data_directory=cli, port=0, with_logging=False)
        assert app.data_dir == cli.resolve()

    def test_env_used_when_no_cli(self, tmp_path, monkeypatch):
        from diangeji import paths
        monkeypatch.setenv("DIANGE_DATA", str(tmp_path / "fromenv"))
        assert paths.data_dir() == (tmp_path / "fromenv").resolve()

    def test_default_when_neither(self, tmp_path, monkeypatch):
        from diangeji import paths
        monkeypatch.delenv("DIANGE_DATA", raising=False)
        assert paths.data_dir().name == paths.DATA_DIR_NAME
