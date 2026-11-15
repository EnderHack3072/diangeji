"""浏览器自动化：启动带调试端口的 Edge，通过 CDP 取 cookie。

原 ``main.py`` 把这段和界面代码搅在一起（还有一处 ``f.write(dict)`` 的截断 bug）。
这里抽成独立类，**不导入 tkinter**，所以可以在无 GUI 环境里测：

* ``CDPClient``  —— 一个最小 CDP 客户端（HTTP 发现 + WebSocket 调用）
* ``BrowserLogin`` —— 启动 Edge / 打开标签 / 取 cookie / 清理现场
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from ..logging_setup import get_logger
from ..utils.ws import WSClient

log = get_logger(__name__)

EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

#: 清掉这几个缓存目录可以治「页面白屏、标题有内容空」
GPU_CACHE_DIRS = (
    r"Default\GPUCache",
    r"Default\Code Cache",
    "ShaderCache",
    "GrShaderCache",
    r"Default\DawnGraphiteCache",
    r"Default\DawnWebGPUCache",
)


def find_edge() -> str | None:
    """找 Edge 可执行文件；找不到返回 ``None``。"""
    for path in EDGE_PATHS:
        if os.path.isfile(path):
            return path
    return None


def clean_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """给子进程一份干净环境。

    PyInstaller 会把 ``_MEIxxxx`` 解包目录塞进 PATH，Edge 继承后可能加载到
    错误的 DLL 导致白屏。这里把相关变量剔掉。
    """
    env = dict(os.environ if environ is None else environ)
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p and "_MEI" not in p]
    env["PATH"] = os.pathsep.join(parts)
    for key in list(env):
        if key == "_MEIPASS" or key.startswith("_PYI") or key in ("PYTHONHOME", "PYTHONPATH"):
            env.pop(key, None)
    return env


def clean_cwd() -> str:
    """给浏览器一个干净的当前目录，别让它在解压目录里找 DLL。"""
    return os.environ.get("SystemRoot", r"C:\Windows")


class CDPClient:
    """Chrome DevTools Protocol 的最小客户端。"""

    def __init__(self, port: int, timeout: float = 2.0) -> None:
        self.port = int(port)
        self.timeout = timeout

    # ---------------- HTTP 发现 ----------------

    def _http_json(self, path: str, method: str = "GET", timeout: float | None = None) -> Any:
        url = f"http://127.0.0.1:{self.port}{path}"
        request = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def alive(self) -> bool:
        try:
            return bool(self._http_json("/json/version", timeout=1.5))
        except Exception:
            return False

    def wait_ready(self, attempts: int = 40, interval: float = 0.6) -> bool:
        for _ in range(attempts):
            if self.alive():
                return True
            time.sleep(interval)
        return False

    def tabs(self) -> list[dict[str, Any]]:
        try:
            data = self._http_json("/json/list")
        except Exception:
            return []
        return data if isinstance(data, list) else []

    def open_tab(self, url: str) -> bool:
        """开一个新标签页。新版本 Edge 要求 PUT，老版本 GET 也行。"""
        encoded = urllib.parse.quote(url, safe=":/?&=%")
        for method in ("PUT", "GET"):
            try:
                self._http_json(f"/json/new?{encoded}", method=method, timeout=5)
                return True
            except Exception:
                continue
        return False

    def find_page(self, host: str) -> dict[str, Any] | None:
        for tab in self.tabs():
            if tab.get("type") == "page" and host in (tab.get("url") or ""):
                return tab
        return None

    def first_page(self) -> dict[str, Any] | None:
        for tab in self.tabs():
            if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl"):
                return tab
        return None

    # ---------------- WebSocket 调用 ----------------

    def call(self, method: str, params: dict | None = None,
             ws_url: str | None = None) -> dict[str, Any]:
        """在页面上下文里执行一条 CDP 命令。"""
        target = ws_url or (self.first_page() or {}).get("webSocketDebuggerUrl")
        if not target:
            raise RuntimeError("没有可用的调试标签页")
        client = WSClient(target)
        client.connect(timeout=8)
        try:
            client.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            deadline = time.time() + 10
            while time.time() < deadline:
                opcode, payload = client.recv()
                if opcode != 1:
                    continue
                message = json.loads(payload.decode("utf-8"))
                if message.get("id") == 1:
                    return message
            raise TimeoutError(f"CDP {method} 超时")
        finally:
            client.close()

    def navigate(self, url: str, ws_url: str | None = None) -> bool:
        try:
            self.call("Page.navigate", {"url": url}, ws_url=ws_url)
            return True
        except Exception as exc:
            log.debug("Page.navigate 失败：%s", exc)
            return False

    def cookies(self, urls: Iterable[str], wanted: Iterable[str]) -> dict[str, str]:
        """取指定域名下、指定名字的 cookie。"""
        self.call("Network.enable")
        result = self.call("Network.getCookies", {"urls": list(urls)})
        cookies = (result.get("result") or {}).get("cookies") or []
        wanted_set = set(wanted)
        return {c["name"]: c["value"] for c in cookies
                if c.get("name") in wanted_set and c.get("value")}


class BrowserLogin:
    """用独立配置目录启动 Edge，登录后取 cookie。"""

    def __init__(self, profile_dir: Path, data_dir: Path,
                 edge_path: str | None = None) -> None:
        self.profile_dir = Path(profile_dir)
        self.data_dir = Path(data_dir)
        self.edge_path = edge_path or find_edge()
        self.process: subprocess.Popen | None = None

    # ---------------- 进程管理 ----------------

    def available(self) -> bool:
        return bool(self.edge_path)

    def launch(self, url: str, port: int, reset: bool = False) -> bool:
        """启动（或在已启动时复用）调试用 Edge。"""
        if not self.edge_path:
            raise RuntimeError("没找到 Edge 浏览器，请先安装 Microsoft Edge")
        if reset:
            self.kill()
            time.sleep(1.5)
            shutil.rmtree(self.profile_dir, ignore_errors=True)
        self.reset_gpu_cache()
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        client = CDPClient(port)
        if client.alive():
            # 调试端口已经开着：直接复用，别再开第二个空白窗
            return True
        try:
            self.process = subprocess.Popen(
                [self.edge_path, f"--user-data-dir={self.profile_dir}",
                 f"--remote-debugging-port={port}", "--no-first-run",
                 "--no-default-browser-check", "--new-window", "--disable-gpu"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
                env=clean_env(), cwd=clean_cwd(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            log.error("启动 Edge 失败：%s", exc)
            return False
        return True

    def kill(self) -> None:
        """只关掉本程序拉起的那个 Edge（带 ``_edgeprofile`` 的），不动用户的浏览器。"""
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
                 "Where-Object { $_.CommandLine -like '*_edgeprofile*' } | "
                 "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                 "-ErrorAction SilentlyContinue }"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass
        self.process = None

    def reset_gpu_cache(self) -> None:
        for relative in GPU_CACHE_DIRS:
            shutil.rmtree(self.profile_dir / relative, ignore_errors=True)

    # ---------------- 登录流程 ----------------

    def login_flow(self, url: str, port: int, wanted: Iterable[str],
                   ready_urls: Iterable[str] | None = None,
                   timeout: float = 180.0,
                   poll_interval: float = 2.0) -> dict[str, str] | None:
        """打开页面，等用户登录，直到 cookie 齐全。

        返回取到的 cookie 字典；超时返回 ``None``。**不做任何落盘**，
        落盘由 :class:`CookieStore` 负责（避免把好文件写坏）。
        """
        wanted_set = list(wanted)
        urls = list(ready_urls or [url])
        host = url.split("//", 1)[-1].split("/")[0]
        client = CDPClient(port)
        if not self.launch(url, port):
            return None
        if not client.wait_ready():
            log.warning("浏览器调试端口没起来")
            return None
        if not self.find_or_open(client, url, host):
            log.warning("登录页没能打开")
            return None

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                got = client.cookies(urls, wanted_set)
            except Exception:
                got = {}
            if wanted_set and all(key in got for key in wanted_set):
                return got
            time.sleep(poll_interval)
        return None

    @staticmethod
    def find_or_open(client: CDPClient, url: str, host: str) -> bool:
        """已有目标网站标签就复用，没有就开一个。"""
        if client.find_page(host):
            return True
        if not client.open_tab(url):
            return False
        for _ in range(20):
            time.sleep(0.8)
            if client.find_page(host):
                return True
        return False
