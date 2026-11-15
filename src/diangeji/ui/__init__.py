"""Tkinter 主播端。

``widgets`` 里是自绘控件（Canvas 圆角、贴图按钮、音量滑块），
``main_window`` 是主窗口与登录窗口 —— 界面逻辑沿用原版，只把「数据从哪来」
换成注入的 :class:`~diangeji.app.Application`。

**这一层之上没有别的层依赖它**，所以 Tk 相关代码可以整体替换而不影响服务端。
"""

from __future__ import annotations

from .main_window import AppWindow, launch

__all__ = ["AppWindow", "launch"]
