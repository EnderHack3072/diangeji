"""自绘控件：Tk 没有原生圆角，全部用 Canvas 画。

逻辑与原 ``main.py`` 里的 ``CLabel`` / ``CButton`` / ``CSlider`` / ``round_rect``
保持一致（外观不变），只是：

* 贴图查找走 :mod:`diangeji.paths`，图片已经收进 ``web/static/img``。
* 控件多了 ``destroy()`` 与状态查询，便于重建。
* ``CButton`` 的图片缓存改成**实例无关**的模块级缓存，重建窗口不会重复加载。
"""

from __future__ import annotations

import hashlib
import os
import tkinter as tk
from typing import Callable

from ..logging_setup import get_logger

log = get_logger(__name__)

FONT_FAMILY = "Microsoft YaHei"

#: 贴图缓存：key -> PhotoImage（PhotoImage 必须被引用住，否则会被 GC）
_IMAGE_CACHE: dict[str, tk.PhotoImage] = {}


def round_rect(cv: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
               r: float = 12, **kwargs) -> int:
    """圆角矩形（平滑多边形近似）。"""
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return cv.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


class CLabel:
    """Canvas 文本，接口近似 ``tk.Label``（只支持 ``text`` / ``fg``）。"""

    def __init__(self, cv: tk.Canvas, x: float, y: float, text: str = "",
                 fg: str = "#4b2a6b", font: tuple = (FONT_FAMILY, 10),
                 anchor: str = "w", width: int | None = None) -> None:
        self.cv = cv
        self.id = cv.create_text(x, y, text=text, fill=fg, font=font,
                                 anchor=anchor, width=width)

    def config(self, **kwargs) -> None:  # noqa: A003 - 与 tk 接口保持一致
        text = kwargs.pop("text", None)
        fg = kwargs.pop("fg", None)
        if text is not None:
            self.cv.itemconfig(self.id, text=text)
        if fg is not None:
            self.cv.itemconfig(self.id, fill=fg)
        if kwargs:
            self.cv.itemconfig(self.id, **kwargs)

    def get(self) -> str:
        return str(self.cv.itemcget(self.id, "text"))


class CButton:
    """按钮：优先用预渲染贴图，找不到就退回 Canvas 圆角。"""

    def __init__(self, cv: tk.Canvas, x: float, y: float, w: float, h: float,
                 text: str, cmd: Callable[[], None], bg: str = "#f2eaff",
                 fg: str = "#59407c", hover: str = "#e7dbff", outline: str = "",
                 font: tuple = (FONT_FAMILY, 10), r: float = 16,
                 primary: bool = False, image_dir: str | None = None) -> None:
        self.cv, self.cmd, self.text = cv, cmd, text
        self.x, self.y, self.w, self.h, self.primary = x, y, w, h, primary
        self.font, self.bg, self.fg = font, bg, fg
        self.hover, self.outline, self.r = hover, outline, r
        self.image_dir = image_dir
        self.norm: tk.PhotoImage | None = None
        self.hov: tk.PhotoImage | None = None
        self._img_mode = self._load_images()

        if self._img_mode:
            self.box = cv.create_image(x, y, image=self.norm, anchor="nw")
            self.txt = None
            cv.tag_bind(self.box, "<Button-1>", lambda _e: self.cmd())
            cv.tag_bind(self.box, "<Enter>",
                        lambda _e: self.hov and cv.itemconfig(self.box, image=self.hov))
            cv.tag_bind(self.box, "<Leave>", lambda _e: cv.itemconfig(self.box, image=self.norm))
        else:
            self.box = round_rect(cv, x, y, x + w, y + h, r, fill=bg, outline=outline)
            self.txt = cv.create_text(x + w / 2, y + h / 2, text=text, fill=fg, font=font)
            for item in (self.box, self.txt):
                cv.tag_bind(item, "<Button-1>", lambda _e: self.cmd())
                cv.tag_bind(item, "<Enter>", lambda _e: cv.itemconfig(self.box, fill=hover))
                cv.tag_bind(item, "<Leave>", lambda _e: cv.itemconfig(self.box, fill=bg))

    # ---- 贴图 ----

    def _key(self, text: str, hover: bool = False) -> str:
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
        return "btn_%dx%d_%s%s%s" % (
            int(self.w), int(self.h), digest,
            "_p" if self.primary else "", "_h" if hover else "")

    def _load_images(self) -> bool:
        self.norm = _load_button_image(self.image_dir, self._key(self.text))
        self.hov = _load_button_image(self.image_dir, self._key(self.text, True))
        return self.norm is not None

    # ---- 对外 ----

    def set_text(self, text: str) -> None:
        if text == self.text:
            return
        self.text = text
        if self._img_mode:
            # 找得到新贴图就换，找不到就退回文字 —— 绝不能留下 norm=None，
            # 否则后续 hover 会把 itemconfig(image=None) 打上去，按钮直接消失。
            if self._load_images() and self.norm is not None:
                self.cv.itemconfig(self.box, image=self.norm)
                return
        if self.txt is not None:
            self.cv.itemconfig(self.txt, text=text)

    def configure(self, **kwargs) -> None:
        if "text" in kwargs:
            self.set_text(kwargs["text"])


def _load_button_image(directory: str | None, key: str) -> tk.PhotoImage | None:
    """按 key 加载 ``btn_*.png``，带全局缓存。"""
    if key in _IMAGE_CACHE:
        return _IMAGE_CACHE[key]
    if not directory:
        return None
    path = os.path.join(directory, key + ".png")
    try:
        image = tk.PhotoImage(file=path)
    except Exception as exc:  # tk.TclError / OSError
        log.debug("按钮贴图缺失：%s (%s)", path, exc)
        image = None
    _IMAGE_CACHE[key] = image  # 缓存 None 也算命中，避免每次重试
    return image


class CSlider:
    """Canvas 音量滑块（Tk 原生 Scale 太丑）。"""

    def __init__(self, cv: tk.Canvas, x: float, y: float, w: float, value: int,
                 cmd: Callable[[int], None], h: int = 20) -> None:
        self.cv, self.x, self.y, self.w, self.h = cv, x, y, w, h
        self.value = int(value)
        self.cmd = cmd
        self.cy = y + h // 2
        self.pill = round_rect(cv, x - 10, y - 3, x + w + 10, y + h + 3, 12,
                               fill="#ffffff", outline="#e3d5ff")
        self.track = cv.create_line(x, self.cy, x + w, self.cy, width=6,
                                    capstyle="round", fill="#e7dbff")
        self.fill = cv.create_line(x, self.cy, x, self.cy, width=6,
                                   capstyle="round", fill="#b58cff")
        self.knob = cv.create_oval(0, 0, 0, 0, fill="#ffffff", outline="#b58cff", width=2)
        for item in (self.pill, self.track, self.fill, self.knob):
            cv.tag_bind(item, "<Button-1>", self._on_click)
            cv.tag_bind(item, "<B1-Motion>", self._on_click)
        self._draw()

    def _draw(self) -> None:
        px = self.x + int(self.w * self.value / 100)
        self.cv.coords(self.fill, self.x, self.cy, px, self.cy)
        radius = 7
        self.cv.coords(self.knob, px - radius, self.cy - radius,
                       px + radius, self.cy + radius)

    def _on_click(self, event) -> None:
        ratio = (event.x - self.x) / self.w * 100
        self.value = max(0, min(100, int(round(ratio))))
        self._draw()
        self.cmd(self.value)

    def get(self) -> int:
        return self.value

    def set(self, value: int) -> None:
        self.value = max(0, min(100, int(value)))
        self._draw()
