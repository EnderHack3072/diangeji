# `ui/` —— Tkinter 界面

`main_window.py` · `widgets.py`

**界面逻辑沿用 v1.0，重构只做了拆分和搬家。** 除了下面「修掉的两个卡顿」
之外，没有任何交互改动。

---

## `widgets.py` —— 自绘控件

```python
def round_rect(cv, x1, y1, x2, y2, radius, **kw)
def _load_button_image(directory, key) -> tk.PhotoImage | None

class CLabel:        # 圆角标签
class CButton:       # 图片按钮（有图用图，没图退化成文字）
class CSlider:       # 自绘滑杆
```

### `CLabel.set_text()` 的坑

它支持两种模式：图片模式（贴 `btn_*.png`）和文字模式。原实现切到文字模式时
会把 `self.norm` 写成 `None`，之后再切回图片模式就崩了。

```python
def set_text(self, text: str) -> None:
    if self._img_mode:
        if self._load_images() and self.norm is not None:
            ...          # 成功贴图
            return
        # 贴图失败 → 落到下面的文字分支，不要把 norm 清成 None
    ...文字模式...
```

按钮图缺失是**正常情况**（用户可能删了图），必须有文字兜底。

---

## `main_window.py` —— 主窗口

```python
class AppWindow:
    def __init__(root: tk.Tk, app: Application)
    def log(text)                  # 线程安全，任何线程都能调
    def refresh_labels()
    def quit()

class QRLoginDialog:
    def __init__(master, client, title, filename, app, on_success=None)

def launch(app, *, args=None) -> int
def _load_config(data_directory) -> Config
```

### `launch()` 为什么必须收 `app`

```python
def launch(app, *, args=None) -> int:
    root = tk.Tk()
    ...
    window = AppWindow(root, app)
```

`app` 是**必填位置参数**，没有 `None` 兜底分支，也**不 import `app.py`**。

理由：`ui` import `app` 就成了「表现层依赖装配层」的反向依赖，
`tests/test_layering.py` 会红。真正的装配在 `__main__.run_gui()` 里做：

```python
app = build_application(...)      # __main__ 装配
launch(app, args=args)            # ui 只负责显示
```

### 线程安全：`_call_ui()` 与 `log()`

Tkinter **不是线程安全的**，所有 UI 操作必须在主线程。所以：

```python
def _call_ui(self, fn) -> None:
    try:
        self.win.after(0, fn)        # 丢回主线程执行
    except Exception:
        self._closed = True          # 窗口没了，别再调 after
```

`log()` 同理，内部是 `root.after(0, write)`。任何后台线程都可以直接
`self.log("...")`，不用自己包 `after`。

### `_guard_async()` —— 别让点按钮卡界面

```python
def _guard_async(self, fn, name, button=None):
    if self._busy: return            # 防重复点击
    self._busy = True
    ...起 daemon 线程跑 fn...
    ...完成后 root.after(0, 复位)...
```

v1.0 的「切歌」「上一首」按钮**在 UI 线程上同步联网**（刷 QQ 直链），
网络一慢整个窗口就白住。改成后台线程 + 按钮置灰，交互没变但不卡了。

`btn_prev` / `btn_next` 现在存成属性，为了置灰时能拿到控件。

### `quit()` —— 顺序很重要

```python
if self._closing: return             # 防重复点「退出」
self._closing = True

grace = 3600 if queue.last_seen_within(10.0) else 0
# 页面还在轮询 = 浏览器开着，等 3.6 秒让最后一句播完

def finish():
    self.log("正在退出…")            # ← 必须还能写日志
    self.app.shutdown()              # 停 HTTP、join 线程
    self.root.after(0, self._destroy)

self.root.after(grace, lambda: threading.Thread(target=finish, daemon=True).start())
```

三处修复：

1. 原实现先 `self._closed = True` 再 `self.log("正在退出…")` ——
   `log()` 看到 `_closed` 直接 return，那行永远不显示。改用独立的
   `_closing` 标志，`_closed` 只留给真正的窗口销毁。
2. `shutdown()` 会停 HTTP、join 线程，可能卡几百毫秒。放在 Tk 回调里
   窗口会白住，所以丢后台线程。
3. 恢复了 v1.0 的退出宽限：页面还在轮询就等 3.6 秒再关，
   否则 OBS 那层瞬间黑屏。`last_seen_within()` 就是为这个存在的。

### `QRLoginDialog` —— 二维码不能卡界面

获取二维码是网络请求（QQ 音乐那步要好几秒）。原实现在 `__init__` 里
同步取，窗口会白住到图出来。

现在：先显示「正在获取二维码…」，再起 `qr-fetch` 线程，
拿到后 `_call_ui(lambda: self._show_qr(...))` 回主线程画。
**只有二维码拿到之后才启动轮询线程。**

---

## 改界面时的注意事项

| 要改什么 | 去哪 |
|---|---|
| 布局坐标 | `_build()` / `_build_login_card()` / `_build_controls()` |
| 背景图 | `_draw_background()` |
| 按钮图 | `web/static/img/buttons/btn_*.png` |
| 字体 | `AppWindow.__init__` 里的 `tkfont.Font(...)` |
| 新增一个动作按钮 | `_build_controls()` + `_guard_async()` + `app` 上的方法 |

新增按钮时**照抄现有按钮的写法**，别自己起线程直接改控件 ——
Tkinter 跨线程改控件是随机崩溃，不是每次都崩。
