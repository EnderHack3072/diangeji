# `web/` —— 本地 HTTP 服务

`routes.py` · `security.py` · `views.py` · `server.py` · `templates/` · `static/`

监听 `127.0.0.1:8770`（可改），给两个页面用：
主播台 `/`（在 Tkinter 里嵌浏览器打开）和 OBS 覆盖层 `/overlay`。

---

## `security.py` —— 本机校验

```python
def parse_host(value) -> str
def parse_origin(value) -> str
def is_loopback(host) -> bool
def is_local_request(headers, *, allow_headless=False) -> bool
```

`/api/*` 的判定规则：

| 情况 | 结果 |
|---|---|
| `Host` 不是回环（`127.0.0.1` / `localhost` / `::1`） | 拒绝 |
| 有 `Origin`/`Referer` 但不是回环 | 拒绝 |
| **没有** `Origin` 也没有 `Referer` | 默认拒绝，`allow_headless=True` 才放行 |

最后一条是**唯一会破坏既有用法的改动**：v1.0 对无头请求是放行的，
挂了 QQ 机器人 / 外部脚本的用户升级后会收到 403。

所以拒绝时不是静默的：

- 403 响应体带 `hint` 字段，直接写明加 `--allow-headless-api`
- 日志区分「Origin 不是回环」和「完全没有浏览器头」两种情况，
  后者每次启动只提示一次

**这个默认拒绝是刻意的**：DNS rebinding 攻击能让外部网页以
`http://evil.com` 的名义打你本机的 `127.0.0.1`。浏览器一定会带 `Origin`，
脚本不会 —— 用这个差异区分。

静态资源（`/media/`、`/asset/`、模板）不校验 `Origin`，因为浏览器
加载 `<img>`/`<audio>` 不带这些头。

---

## `routes.py` —— 路由与处理器

```python
@dataclass
class AppContext:
    playback: PlaybackService      # 业务全靠它
    library: LocalLibrary
    template_dir / image_dir / button_dir
    danmaku_state: Callable | None
    version: str
    allow_headless: bool = False

class RequestHandler(BaseHTTPRequestHandler): ...
def make_handler(ctx) -> type[RequestHandler]     # 把 ctx 绑进类
class AppServer(ThreadingHTTPServer): ...
def build_server(ctx, port=8770, ...) -> AppServer
```

`AppContext` 是**依赖注入的载体** —— 测试时可以塞假 `playback`，
不用起真队列。

### 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 主播台（注入初始状态） |
| GET | `/overlay` | OBS 覆盖层 |
| GET | `/api/state` | 完整状态快照 |
| GET | `/api/library` | 曲库概要 |
| GET | `/media/<rel>` | 音频流，支持 Range |
| GET | `/asset/<name>` | 图片（只允许 png/ico） |
| POST | `/api/order` | `{"user": "...", "text": "点歌 晴天"}` |
| POST | `/api/ended` | `{"id": "<曲目id>"}` 上报播完 |
| POST | `/api/skip` `/api/next` | 下一首 |
| POST | `/api/prev` | 上一首 |
| POST | `/api/pause` `/api/volume` `/api/idle` | 控制 |
| POST | `/api/remove` `/api/clear` | 队列操作 |

### 几个实现细节

**① `_drain()` 先读掉请求体再返回 403**

HTTP/1.1 是长连接。POST 请求被拒时不读请求体，那堆字节还留在 socket 里，
下一个请求解析就会错位。所以 `do_POST` 里先 `_drain()` 再 `_forbidden()`。

**② Range 支持（`_parse_range()`）**

音频拖动进度条必须有 206。`_parse_range()` 处理 `bytes=0-`、`bytes=-500`、
多段（不支持，退化成一整块）等写法，非法值返回 `None` → 416。

**③ `HEAD` 请求**

`_media()` 在 `HEAD` 时发完头就 `return`，不写 body。

**④ `AppServer` 的端口冲突检测**

```python
if os.name == "nt":
    self.allow_reuse_address = False        # Windows 上 REUSEADDR 是陷阱
    self.socket.setsockopt(SOL_SOCKET, SO_EXCLUSIVEADDRUSE, 1)
```

Windows 的 `SO_REUSEADDR` 允许**两个 socket 绑同一端口** —— 于是「端口被占用」
永远检测不出来，两个点歌姬会各绑一个，请求随机落到其中一个，表现就是
「点歌没反应」。POSIX 的语义不同，所以按平台分开处理。

**⑤ 异常分层**

```python
except DianGeJiError as exc:        # 业务异常 → 400 + 人话
    self._json({"ok": False, "error": str(exc)}, 400)
except Exception:                   # 其他 → 500，栈只进日志
    log.exception(...)
```

---

## `views.py` —— 模板渲染

```python
def render_console(template_dir, state, lib_count, version) -> str
def render_overlay(template_dir, version="") -> str
def render_state_script(state) -> str
```

- 纯字符串替换，**没有模板引擎**（不引入 Jinja 依赖）。
- 占位符：`__STATE__`（初始状态 JSON）、`__LIBCOUNT__`、`__VERSION__`。
- 模板里 `<script>` 内联的状态用 `render_state_script()` 生成，
  会转义 `</script>` 之类的危险序列。

模板文件在 `templates/`，图片在 `static/img/`（含 `buttons/` 24 张按钮图）。
这些都在 `src/` 里面，打包时由 `scripts/build.py` 一起带上。

---

## `server.py` —— 生命周期

```python
class HttpService:
    def start() -> None
    def stop() -> None
    @property url / port
```

- `start()` 在后台线程跑 `serve_forever()`。
- `stop()` 先 `shutdown()` 再 `server_close()`。
- 有一处必须注意：`_serve()` 里要**先把 `self._server` 抓成局部变量**
  再进 try。否则 `start()` 失败时 `_server` 还是 `None`，
  异常处理路径会再抛一个 `AttributeError`，把真正的错误盖掉。

线程模型见 [architecture.md](../architecture.md) 的线程表。

---

## 前端两个页面的共同约定

- 都轮询 `/api/state`（2 秒一次），并且**按 `id` 去重**换歌 ——
  不能只看 URL 变没变，vkey 直链会变但歌没变。
- 播完上报 `/api/ended` 时**必须带 id**，服务端靠它做 CAS。
- 主播台页面静音（`mute=1`），声音由 OBS 那层出。
