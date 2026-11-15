"""主窗口：登录区 / 播放控制 / 日志，逻辑与原 ``main.py`` 的 ``App`` 一致。

修掉的界面层 bug：

* 扫码窗口的轮询线程不再直接改 Tk 控件，全部 ``root.after(0, ...)`` 回主线程。
* 启动不再无条件 ``after(400, self.start)`` 甩一个模态警告，
  只有 ``config.auto_start`` 或 ``--room`` 给了房间号才自动启动。
* 「浏览器登录 QQ」不再自己拼 cookie 字符串，交给 :class:`CookieStore`
  原子写入（原版 ``f.write(dict)`` 会把已有 cookie 截成空文件）。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox
from typing import TYPE_CHECKING

from .. import __version__
from ..auth.browser import find_edge
from ..auth.qrcode_login import BiliQRLogin, LoginResult, QQMusicQRLogin, QRPayload
from ..auth.store import BILI_COOKIE, QQ_COOKIE
from ..config import Config
from ..constants import APP_NAME, CDP_PORT_QQ, DEFAULT_PORT
from ..logging_setup import get_logger
from ..paths import button_image_dir, image_dir
from ..paths import data_dir as default_data_dir
from .widgets import CButton, CLabel, CSlider, round_rect

if TYPE_CHECKING:  # 只为类型标注；运行时不 import 组合根，避免反向依赖
    from ..app import Application

log = get_logger(__name__)

WINDOW_W, WINDOW_H = 700, 640
CANVAS_W, CANVAS_H = 716, 646

INK = "#4b2a6b"
SUB = "#8b7aa5"
MUTED = "#9a89b3"
PINK = "#c86bb0"
ACCENT = "#b58cff"
CARD = "#ffffff"
LINE = "#e3d5ff"
BG_TOP, BG_BOT = (247, 241, 255), (231, 217, 255)


class QRLoginDialog:
    """扫码登录窗口。轮询在后台线程，所有界面更新回主线程。"""

    def __init__(self, master: tk.Misc, client, title: str, filename: str,
                 app: Application, on_success=None) -> None:
        self.client = client
        self.filename = filename
        self.app = app
        self.on_success = on_success

        self.win = tk.Toplevel(master)
        self.win.title(f"扫码登录 {title}")
        self.win.configure(bg="#221a33")
        self.win.resizable(False, False)
        self._closed = False
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        # 先摆一句提示，二维码在后台取：client.start() 要发两次 HTTP（最坏 15 秒），
        # 在 UI 线程里同步跑会让点「扫码登录」的瞬间整个窗口白住。
        self.status = tk.Label(self.win, text="正在获取二维码…", bg="#221a33",
                               fg="#b9aecf", font=("Microsoft YaHei", 10))
        self.status.pack(padx=40, pady=(40, 40))
        self.win.update_idletasks()
        self.win.geometry(f"+{master.winfo_rootx() + 120}+{master.winfo_rooty() + 80}")

        threading.Thread(target=self._fetch_qr, args=(title,),
                         name="qr-fetch", daemon=True).start()

    def _fetch_qr(self, title: str) -> None:
        """后台取二维码，取到后回主线程画。"""
        try:
            payload: QRPayload = self.client.start()  # type: ignore[attr-defined]
        except Exception as exc:
            self._call_ui(lambda: self._fail(f"取二维码失败：{exc}"))
            return
        self._call_ui(lambda: self._show_qr(payload, title))

    def _fail(self, message: str) -> None:
        if self._closed:
            return
        try:
            self.win.destroy()
        except Exception:
            pass
        messagebox.showerror(APP_NAME, message)

    def _show_qr(self, payload: QRPayload, title: str) -> None:
        if self._closed:
            return
        self.status.destroy()
        self._draw_qr(payload)
        tk.Label(self.win, text=f"用{title} App 里的「扫一扫」扫，别用系统相机",
                 bg="#221a33", fg="#e8ddff",
                 font=("Microsoft YaHei", 11)).pack(pady=(0, 4))
        self.status = tk.Label(self.win, text="等待扫码…", bg="#221a33", fg="#b9aecf",
                               font=("Microsoft YaHei", 10))
        self.status.pack(pady=(0, 16))
        self.win.update_idletasks()
        threading.Thread(target=self._poll_loop, name="qr-poll", daemon=True).start()

    # ---------------- 绘制 ----------------

    def _draw_qr(self, payload: QRPayload) -> None:
        matrix = payload.matrix
        if not matrix:
            tk.Label(self.win, text=payload.error or "二维码生成失败，重开一次",
                     bg="#221a33", fg="#fff").pack(padx=20, pady=20)
            return
        size = len(matrix)
        cell = max(2, 264 // max(1, size))
        pixels = cell * size
        canvas = tk.Canvas(self.win, width=pixels, height=pixels, bg="white",
                           highlightthickness=0, bd=0)
        canvas.pack(padx=20, pady=(18, 6))
        for y, row in enumerate(matrix):
            for x, dark in enumerate(row):
                if dark:
                    canvas.create_rectangle(x * cell, y * cell, (x + 1) * cell,
                                            (y + 1) * cell, fill="black", outline="")

    # ---------------- 轮询 ----------------

    def _set_status(self, text: str) -> None:
        self._call_ui(lambda: (self.status.configure(text=text)) if not self._closed else None)

    def _call_ui(self, fn) -> None:
        """从后台线程安全地回到主线程执行。"""
        try:
            self.win.after(0, fn)
        except Exception:  # 窗口已销毁
            self._closed = True

    def _poll_loop(self) -> None:
        deadline = time.time() + 180
        while time.time() < deadline and not self._closed:
            time.sleep(2)
            if self._closed:
                return
            try:
                state, cookie = self.client.poll()  # type: ignore[attr-defined]
            except Exception as exc:
                self._set_status(f"出错了：{exc}")
                return
            if state is LoginResult.SCANNED:
                self._set_status("已扫码，请在手机上确认")
            elif state is LoginResult.EXPIRED:
                self._set_status("二维码过期了，关掉重开一次")
                return
            elif state is LoginResult.OK and cookie:
                ok = self.app.cookies.write(self.filename, cookie)
                self._call_ui(lambda: self._finish(ok))
                return
        self._set_status("超时了，关掉重开一次")

    def _finish(self, ok: bool) -> None:
        if not ok:
            self.status.configure(text="登录成功但保存失败，检查数据目录权限")
            return
        self.app.log(f"{self.win.title()[5:]} 登录成功")
        if self.on_success is not None:
            try:
                self.on_success()
            except Exception:  # pragma: no cover
                pass
        if not self._closed:
            self.win.destroy()
            self._closed = True

    def close(self) -> None:
        self._closed = True
        try:
            self.win.destroy()
        except Exception:
            pass


class AppWindow:
    """主窗口。"""

    def __init__(self, root: tk.Tk, app: Application) -> None:
        self.root = root
        self.app = app
        self.app.on_log = self.log
        root.title(f"{APP_NAME} v{__version__}")
        root.geometry(f"{WINDOW_W}x{WINDOW_H}")
        root.configure(bg="#f6efff")
        root.resizable(False, False)
        self._closed = False
        self._closing = False    # 退出流程已启动（防重复点「退出」）
        self._busy = False       # 后台动作（切歌等）进行中，防止重复点击
        import tkinter.font as tkfont
        self._font = tkfont.Font(family="Microsoft YaHei", size=11, weight="bold")
        self._build()
        self.refresh_labels()

    # ---------------- 布局 ----------------

    def _build(self) -> None:
        import tkinter.font as tkfont

        cv = tk.Canvas(self.root, width=CANVAS_W, height=CANVAS_H,
                       highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)
        self.cv = cv
        self._draw_background(cv, CANVAS_H)

        title_font = tkfont.Font(family="Microsoft YaHei", size=20, weight="bold")
        x = 18
        for char, color in (("点", INK), ("歌", PINK), ("姬", INK)):
            cv.create_text(x, 30, text=char, font=title_font, fill=color, anchor="w")
            x += title_font.measure(char)
        cv.create_text(18, 58, text="观众发「点歌 歌名」→ 排队 → 播放 → OBS 显示",
                       font=("Microsoft YaHei", 9), fill=SUB, anchor="w")

        self._build_login_card(cv)
        self._build_controls(cv)
        self._build_log(cv)

        self.log("准备就绪。填房间号 → 登录两个账号 → 启动。")
        self.root.after(1500, self._tick_now)

    def _draw_background(self, cv: tk.Canvas, height: int) -> None:
        bg_path = os.path.join(str(image_dir()), "bg.png")
        try:
            self.bg_img = tk.PhotoImage(file=bg_path)
            cv.create_image(0, 0, image=self.bg_img, anchor="nw")
        except Exception as exc:
            log.debug("背景图不可用，退回渐变：%s", exc)
            for i in range(height):
                ratio = i / (height - 1)
                color = "#%02x%02x%02x" % tuple(
                    int(BG_TOP[k] + (BG_BOT[k] - BG_TOP[k]) * ratio) for k in range(3))
                cv.create_line(0, i, CANVAS_W, i, fill=color)

    def _build_login_card(self, cv: tk.Canvas) -> None:
        buttons = str(button_image_dir())
        round_rect(cv, 18, 74, 698, 258, 16, fill=CARD, outline=LINE)
        cv.create_text(32, 100, text="直播间房间号", font=("Microsoft YaHei", 10),
                       fill="#6c5a86", anchor="w")
        round_rect(cv, 120, 86, 320, 116, 15, fill="#faf7ff", outline="#dccbff")
        self.room_var = tk.StringVar(value=self.app.config.room_id)
        entry = tk.Entry(self.root, textvariable=self.room_var, font=("Consolas", 12),
                         bg="#faf7ff", fg="#3b2a52", insertbackground="#3b2a52",
                         relief="flat", bd=0, highlightthickness=0)
        entry.place(x=132, y=90, width=176, height=22)

        self.bili_lbl = CLabel(cv, 32, 136, "", fg=SUB, font=("Microsoft YaHei", 9))
        self.qq_lbl = CLabel(cv, 32, 158, "", fg=SUB, font=("Microsoft YaHei", 9))

        CButton(cv, 32, 174, 124, 32, "① 扫码登录 B 站", self.login_bili,
                primary=True, image_dir=buttons)
        CButton(cv, 164, 174, 142, 32, "② 扫码登录 QQ 音乐", self.login_qq,
                image_dir=buttons)
        CButton(cv, 314, 174, 130, 32, "③ 浏览器登录 QQ", self.login_qq_browser,
                image_dir=buttons)
        CButton(cv, 610, 174, 72, 32, "退出", self.quit, image_dir=buttons)
        CButton(cv, 32, 214, 140, 32, "④ 手动粘贴 Cookie", self.paste_qq_cookie,
                image_dir=buttons)
        cv.create_text(182, 230, text="（浏览器登录不出画面时用这个）",
                       font=("Microsoft YaHei", 8), fill=MUTED, anchor="w")

    def _build_controls(self, cv: tk.Canvas) -> None:
        buttons = str(button_image_dir())
        CButton(cv, 18, 270, 68, 32, "启动", self.start, primary=True, image_dir=buttons)
        CButton(cv, 94, 270, 76, 32, "主播台", self.open_console, image_dir=buttons)
        CButton(cv, 178, 270, 76, 32, "OBS 层", self.open_overlay, image_dir=buttons)

        self.btn_prev = CButton(cv, 18, 310, 62, 30, "上一首", self._previous,
                                image_dir=buttons)
        self.btn_pause = CButton(cv, 88, 310, 62, 30, "暂停", self._toggle_pause,
                                 image_dir=buttons)
        self.btn_next = CButton(cv, 158, 310, 62, 30, "下一首", self._next,
                                image_dir=buttons)
        cv.create_text(256, 326, text="音量", font=("Microsoft YaHei", 9),
                       fill="#6c5a86", anchor="w")
        self.vol = CSlider(cv, 306, 315, 84, self.app.config.volume, self._on_volume)
        self.vol_lbl = CLabel(cv, 408, 326, str(self.app.config.volume),
                              fg="#59407c", font=("Microsoft YaHei", 9))

        self.now_lbl = CLabel(cv, 18, 366, "当前播放：—", fg="#a03f7c",
                              font=("Microsoft YaHei", 11, "bold"))
        self.now_sub = CLabel(cv, 18, 390, "", fg=SUB, font=("Microsoft YaHei", 8))
        cv.create_text(18, 410,
                       text="全局快捷键 Ctrl+Alt：空格 暂停/继续   ← → 上一首/下一首   ↑↓ 音量",
                       font=("Microsoft YaHei", 8), fill=MUTED, anchor="w")
        self.state_lbl = CLabel(cv, 18, 432, "未启动", fg="#6c5a86",
                                font=("Microsoft YaHei", 10))
        self._install_hotkeys()

    def _build_log(self, cv: tk.Canvas) -> None:
        round_rect(cv, 18, 448, 470, 632, 14, fill=CARD, outline="#e6d9ff")
        self.log_box = tk.Text(self.root, bg="#ffffff", fg="#7b6a94", relief="flat",
                               bd=0, highlightthickness=0, font=("Consolas", 9),
                               insertbackground="#3b2a52", padx=4, pady=4,
                               state="disabled")
        self.log_box.place(x=30, y=460, width=428, height=160)

    # ---------------- 日志 ----------------

    def log(self, text: str) -> None:
        """线程安全：可以从任何线程调用。"""
        def write() -> None:
            if self._closed:
                return
            self.log_box.configure(state="normal")
            self.log_box.insert("end", time.strftime("[%H:%M:%S] ") + text + "\n")
            lines = int(self.log_box.index("end-1c").split(".")[0])
            if lines > 400:
                self.log_box.delete("1.0", f"{lines - 400}.0")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        try:
            self.root.after(0, write)
        except Exception:  # 窗口已销毁
            self._closed = True

    # ---------------- 顶部状态 ----------------

    def refresh_labels(self) -> None:
        has_bili = self.app.cookies.has(BILI_COOKIE)
        has_qq = self.app.cookies.has(QQ_COOKIE)
        self.bili_lbl.config(text="B 站：已登录" if has_bili else "B 站：未登录",
                             fg="#8fd6a8" if has_bili else "#e08a8a")
        self.qq_lbl.config(
            text="QQ 音乐：已登录" if has_qq else "QQ 音乐：未登录（缺会员曲）",
            fg="#8fd6a8" if has_qq else "#e08a8a")
        self.app.refresh_sources()

    # ---------------- 播放控制 ----------------

    def _tick_now(self) -> None:
        try:
            state = self.app.playback.snapshot()
            playing = state.get("playing")
            if playing:
                self.now_lbl.config(text=self._fit("当前播放：" + str(playing.get("title") or "—")))
                bits = ["空闲随机" if playing.get("idle")
                        else f'{playing.get("user") or "观众"} 点']
                bits.append("QQ 音乐" if playing.get("source") == "qq" else "本地曲库")
                if not playing.get("path"):
                    bits.append("缺音源")
                if state.get("paused"):
                    bits.append("已暂停")
                pending = len(state.get("queue") or [])
                if pending:
                    bits.append(f"队列 {pending} 首")
                self.now_sub.config(text=" · ".join(bits))
            else:
                self.now_lbl.config(text="当前播放：—")
                self.now_sub.config(text="空闲随机" if state.get("idle") else "等待点歌")
        except Exception:  # pragma: no cover
            pass
        finally:
            if not self._closed:
                try:
                    self.root.after(2000, self._tick_now)
                except Exception:
                    pass

    def _fit(self, text: str, limit: int = 430) -> str:
        """按像素宽度截断，超了补省略号（避免长歌名压到右边立绘上）。"""
        try:
            font = self._font
            if font.measure(text) <= limit:
                return text
            shortened = text
            while shortened and font.measure(shortened + "…") > limit:
                shortened = shortened[:-1]
            return shortened + "…"
        except Exception:  # pragma: no cover
            return text

    def _guard(self, fn, name: str) -> None:
        try:
            fn()
            self.log(name + " 完成")
        except Exception as exc:
            self.log(f"{name} 失败：{exc}")

    def _guard_async(self, fn, name: str, button=None) -> None:
        """按钮动作一律丢到后台线程。

        ``playback.next()`` / ``previous()`` 会刷新 QQ 音乐直链（联网，最坏 30 秒），
        队列空了还会去取「我喜欢」。在 Tk 回调里同步跑 = 界面直接冻死。
        """
        if getattr(self, "_busy", False):
            return
        if button is not None:
            try:
                button.configure(state="disabled")
            except Exception:
                button = None
        self._busy = True

        def work() -> None:
            try:
                fn()
                self.root.after(0, lambda: self.log(name + " 完成"))
            except Exception as exc:
                self.root.after(0, lambda: self.log(f"{name} 失败：{exc}"))
            finally:
                def done() -> None:
                    self._busy = False
                    if button is not None:
                        try:
                            button.configure(state="normal")
                        except Exception:
                            pass
                self.root.after(0, done)

        threading.Thread(target=work, name=f"ui-{name}", daemon=True).start()

    def _next(self) -> None:
        self._guard_async(self.app.playback.next, "下一首", self.btn_next)

    def _previous(self) -> None:
        self._guard_async(self.app.playback.previous, "上一首", self.btn_prev)

    def _toggle_pause(self) -> None:
        state = self.app.playback.set_paused()
        self.btn_pause.configure(text="继续" if state.get("paused") else "暂停")

    def _on_volume(self, value) -> None:
        try:
            self.app.playback.set_volume(int(float(value)))
            self.vol_lbl.configure(text=str(int(float(value))))
        except Exception:  # pragma: no cover
            pass

    def bump_volume(self, delta: int) -> None:
        value = max(0, min(100, int(self.vol.get()) + delta))
        self.vol.set(value)
        try:
            self.app.playback.set_volume(value)
        except Exception:  # pragma: no cover
            pass

    # ---------------- 全局快捷键 ----------------

    def _install_hotkeys(self) -> None:
        """Ctrl+Alt+空格/←→/↑↓。Windows 专用，失败只记日志。"""
        try:
            import ctypes
        except ImportError:  # pragma: no cover - 非 Windows
            return
        if not hasattr(ctypes, "windll"):
            return

        MOD_ALT, MOD_CONTROL, WM_HOTKEY = 0x0001, 0x0002, 0x0312
        keys = {"space": 0x20, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28}
        actions = {
            1: lambda: self.root.after(0, self._toggle_pause),
            2: lambda: self.root.after(0, self._previous),
            3: lambda: self.root.after(0, self._next),
            4: lambda: self.root.after(0, self.bump_volume, 5),
            5: lambda: self.root.after(0, self.bump_volume, -5),
        }
        order = [(1, keys["space"]), (2, keys["left"]), (3, keys["right"]),
                 (4, keys["up"]), (5, keys["down"])]

        class _MSG(ctypes.Structure):
            _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                        ("wParam", ctypes.c_void_p), ("lParam", ctypes.c_void_p),
                        ("time", ctypes.c_uint), ("pt", ctypes.c_long * 2)]

        def loop() -> None:
            user32 = ctypes.windll.user32
            registered = 0
            for hotkey_id, vk in order:
                if user32.RegisterHotKey(None, hotkey_id, MOD_ALT | MOD_CONTROL, vk):
                    registered += 1
            self.log("全局快捷键已生效（Ctrl+Alt+空格/←→/↑↓）" if registered
                     else "全局快捷键注册失败（可能被别的程序占用）")
            msg = _MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    action = actions.get(int(msg.wParam or 0))
                    if action:
                        try:
                            action()
                        except Exception:  # pragma: no cover
                            pass

        threading.Thread(target=loop, name="hotkeys", daemon=True).start()

    # ---------------- 登录 ----------------

    def login_bili(self) -> None:
        QRLoginDialog(self.root, BiliQRLogin(), "B 站", BILI_COOKIE, self.app,
                      on_success=self.refresh_labels)

    def login_qq(self) -> None:
        QRLoginDialog(self.root, QQMusicQRLogin(), "QQ 音乐", QQ_COOKIE, self.app,
                      on_success=self.refresh_labels)

    def login_qq_browser(self) -> None:
        """备用：浏览器登录 QQ 音乐，再通过 CDP 取 cookie（原子写入）。"""
        def work() -> None:
            try:
                self.log("浏览器已打开，请在窗口里登录 QQ 音乐（登录完不用关）")
                got = self.app.browser.login_flow(
                    url="https://y.qq.com/", port=CDP_PORT_QQ,
                    wanted=["qm_keyst"],
                    ready_urls=["https://y.qq.com/", "https://graph.qq.com/"],
                )
                if not got:
                    self.log("等了三分钟还没登录成功，再点一次试试")
                    return
                if self.app.cookies.write(QQ_COOKIE, got):
                    self.log("QQ 音乐 登录成功（浏览器）")
                    self.root.after(0, self.refresh_labels)
                else:
                    self.log("取到 cookie 但保存失败，检查数据目录权限")
            except Exception as exc:
                self.log(f"浏览器登录失败：{exc}")

        threading.Thread(target=work, name="qq-browser-login", daemon=True).start()

    def paste_qq_cookie(self) -> None:
        """手动粘贴 cookie（浏览器登录不出画面时的兜底）。"""
        win = tk.Toplevel(self.root)
        win.title("粘贴 QQ 音乐 Cookie")
        win.configure(bg="#1b1424")
        win.geometry("620x340")
        tk.Label(win, text="① 浏览器打开 y.qq.com 并登录\n"
                           "② 按 F12 → Application（应用）→ Cookies → https://y.qq.com\n"
                           "③ 找到 qm_keyst 和 uin，把两行连同名字一起粘进来，形如：\n"
                           "    qm_keyst=xxxxx; uin=123456789",
                 bg="#1b1424", fg="#f0eaff", justify="left",
                 font=("Microsoft YaHei", 10)).pack(padx=14, pady=(12, 6), anchor="w")
        box = tk.Text(win, height=6, font=("Consolas", 10), bg="#2a1f3a", fg="#fff",
                      insertbackground="#3b2a52", relief="flat")
        box.pack(fill="x", padx=14)
        tip = tk.Label(win, text="", bg="#1b1424", fg="#e08a8a",
                       font=("Microsoft YaHei", 9))
        tip.pack(pady=6, anchor="w", padx=14)

        def save() -> None:
            raw = box.get("1.0", "end").strip()
            pairs = {}
            for part in raw.replace("\n", ";").split(";"):
                key, sep, value = part.partition("=")
                if sep and key.strip():
                    pairs[key.strip()] = value.strip()
            if "qm_keyst" not in pairs:
                tip.config(text="没找到 qm_keyst，检查一下复制的内容")
                return
            if self.app.cookies.write(QQ_COOKIE, pairs):
                self.log("QQ 音乐 登录成功（手动粘贴）")
                self.refresh_labels()
                win.destroy()
            else:
                tip.config(text="保存失败，检查数据目录权限")

        tk.Button(win, text="保存", command=save, bg="#c86bb0", fg="white",
                  relief="flat", padx=22, pady=5).pack(pady=8)

    # ---------------- 运行 ----------------

    def start(self) -> None:
        if self.app.http.running:
            self.log("已经在运行了，不用再点")
            return
        room = (self.room_var.get() or "").strip()
        if not room.isdigit():
            messagebox.showwarning(APP_NAME, "先填直播间房间号（纯数字）")
            return
        self.app.config.room_id = room
        self.state_lbl.config(text="启动中…", fg="#ffd479")

        def boot() -> None:
            ok = self.app.start(room)
            if ok:
                self.root.after(0, lambda: self.state_lbl.config(
                    text=f"运行中 · 房间 {room}", fg="#8fd6a8"))
            else:
                self.root.after(0, lambda: self.state_lbl.config(
                    text="启动失败，看日志", fg="#e08a8a"))

        threading.Thread(target=boot, name="boot", daemon=True).start()

    def _open_url(self, url: str) -> None:
        """依次尝试：Edge → 系统默认浏览器 → explorer。"""
        edge = find_edge()
        tries = []
        if edge:
            tries.append(("Edge", lambda: subprocess.Popen([edge, url], close_fds=True)))
        tries.append(("系统默认浏览器", lambda: webbrowser.open(url)))
        tries.append(("explorer", lambda: subprocess.Popen(["explorer.exe", url], close_fds=True)))
        last_error = None
        for name, action in tries:
            try:
                action()
                self.log(f"已用 {name} 打开：{url}")
                return
            except Exception as exc:  # pragma: no cover
                last_error = exc
        self.log(f"打开浏览器失败：{last_error}（可以手动打开 {url}）")

    def open_console(self) -> None:
        if not self.app.http.running:
            self.log("先点「启动」")
            return
        self._open_url(self.app.console_url(mute=True))

    def open_overlay(self) -> None:
        if not self.app.http.running:
            self.log("先点「启动」")
            return
        self._open_url(self.app.overlay_url())

    def quit(self) -> None:
        """退出：先给页面一点收尾时间，再在后台真正关服务。

        ``shutdown()`` 要停 HTTP、join 线程，可能卡几百毫秒 —— 放在 Tk 回调里
        会让窗口白住一下，所以丢后台，完成后回主线程销毁窗口。
        """
        if self._closing:            # 已经点过退出，别重复来一遍
            return
        self._closing = True

        # 浏览器页面还在轮询 = 它开着。旧版会等 3.6 秒让它把最后一句播完，
        # 直接 destroy 会让 OBS 那一层瞬间黑屏。
        try:
            grace = 3600 if self.app.playback.queue.last_seen_within(10.0) else 0
        except Exception:  # pragma: no cover
            grace = 0
        if grace:
            self.log("页面还连着，先让最后一段播完…")

        def finish() -> None:
            self.log("正在退出…")
            try:
                self.app.shutdown()
            except Exception as exc:  # pragma: no cover
                log.exception("退出时出错")
                self.log(f"退出时有异常：{exc}")
            try:
                self.root.after(0, self._destroy)
            except Exception:
                pass

        def start() -> None:
            threading.Thread(target=finish, name="shutdown", daemon=True).start()

        try:
            self.root.after(grace, start)
        except Exception:  # pragma: no cover - 窗口已没了
            start()

    def _destroy(self) -> None:
        self._closed = True
        try:
            self.root.destroy()
        except Exception:  # pragma: no cover
            pass


def launch(app, *, args=None) -> int:
    """创建根窗口并进入主循环。

    ``app`` 由调用方（``__main__``）装配好传进来 —— 界面层**不 import**
    ``app.py``，否则就成了「表现层依赖装配层」的反向依赖。
    """
    root = tk.Tk()
    root.title(f"{APP_NAME} v{__version__}")
    root.configure(bg="#f6efff")
    root.geometry(f"{WINDOW_W}x{WINDOW_H}")
    root.resizable(False, False)

    def attach() -> None:
        window = AppWindow(root, app)
        root.protocol("WM_DELETE_WINDOW", window.quit)
        room = getattr(args, "room", "") or app.config.room_id
        if room and app.config.auto_start:
            root.after(600, window.start)

    root.after(0, attach)
    root.mainloop()
    return 0


def _load_config(data_directory):
    """界面启动时读一次配置（路径与 ``build_application`` 保持一致）。"""
    directory = data_directory or default_data_dir()
    return Config.load(Path(directory) / "config.json")
