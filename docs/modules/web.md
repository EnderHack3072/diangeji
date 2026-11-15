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
def is_host_allowed(headers) -> bool          # 新增
def is_local_request(headers, *, allow_headless=False) -> bool
```

判定是**两步**，顺序不能反：

| 步骤 | 覆盖范围 | 规则 |
|---|---|---|
| 1. `is_host_allowed()` | **所有**路径（页面、图片、模板、API） | `Host` 必须解析成回环地址，否则 403 |
| 2. `is_local_request()` | 只有 `/api/*` | 再查 `Origin`/`Referer` |

第 1 步是后补的：原来只挡 `/api/*`，于是伪造 `Host: evil.com` 能直接读到
主播台 HTML —— 那份 HTML 里带着完整队列和观众昵称。实测修前
`GET /` + `Host: evil.com` 返回 200、13610 字节。

`/api/*` 在第 2 步的判定规则：

| 情况 | 结果 |
|---|---|
| `Host` 不是回环（`127.0.0.1` / `localhost` / `::1`） | 拒绝（第 1 步已拦） |
| 有 `Origin`/`Referer` 但不是回环 | 拒绝 |
| **没有** `Origin` 也没有 `Referer` | 默认拒绝，`allow_headless=True` 才放行 |

> `::1` 与 `[::1]` 都在 `constants.LOCAL_HOSTS` 里 —— **唯一来源**。
> Windows 上 `localhost` 常先解析到 `::1`，漏掉会让浏览器自己打开
> `http://localhost:8770` 直接吃 403。

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
| GET | `/api/state` | 状态快照（轻量：不带 `log`，队列最多 50 条） |
| GET | `/api/library` | 曲库概要 |
| GET | `/media/<rel>` | 音频流，支持 Range → `206` + `Content-Range` |
| GET | `/asset/<name>` | 图片（只允许 png/ico） |
| POST | `/api/order` | `{"user": "...", "text": "点歌 晴天"}` |
| POST | `/api/ended` | `{"id": "<曲目id>"}` 上报播完 |
| POST | `/api/skip` `/api/next` | 下一首 |
| POST | `/api/prev` | 上一首 |
| POST | `/api/pause` `/api/volume` `/api/idle` | 控制 |

### `/api/state` 为什么是「轻量」的

`PlayQueue._lock` 保护 `playing/queue/history`，而快照要把每一条
`to_dict()` 一遍 —— **全部在锁内**。前端每 2 秒轮询一次，热门直播间
队列几百条时，点歌入队会被轮询挡在后面。

所以 `queue.snapshot(include_log=..., limit=...)`：

- `include_log=False` —— 日志只有界面那个日志框要，轮询不必带最近 40 条。
  模板对 `s.log` 做了 `|| []` 兜底，少字段不会出错。
- `limit=50` —— 前端本来就只渲染前几十条。

**默认参数仍是「全都要」**，`GET /` 首屏注入和所有老调用点行为不变。

### Range / 206 / 416 的三条规则

| 请求的 `Range` | 响应 |
|---|---|
| 没有，或不是 `bytes=` 开头 | `200` 全量，**不带** `Content-Range` |
| 正常区间，覆盖整个文件 | `200`（没必要切片） |
| 正常区间，真正的部分 | `206` + `Content-Range: bytes a-b/size` |
| 畸形（`bytes=`、`bytes=--`、非数字） | **忽略头**，按 `200` 全量 |

最后一条是后修的：原实现 `except ValueError` 落到 `return None` → 走 416，
播放器拿到 416 直接放弃不播。RFC 9110 说畸形 `Range` 应当**忽略**，
不是报错。

`206` 与 `Content-Range` 必须**同时出现或同时不出现**：原实现无条件发
`Content-Range` 却回 200，浏览器据此判断「服务器支持分段」，seek 就乱了。
| POST | `/api/remove` `/api/clear` | 队列操作 |

### 几个实现细节

**① `_drain()` 先读掉请求体再返回 403**

HTTP/1.1 是长连接。POST 请求被拒时不读请求体，那堆字节还留在 socket 里，
下一个请求解析就会错位。所以 `do_POST` 里先 `_drain()` 再 `_forbidden()`。

**② Range 支持（`_parse_range()` / `_is_partial()`）**

音频拖动进度条必须有 206。`_parse_range()` 处理 `bytes=0-`、`bytes=-500`、
多段（不支持，退化成一整块）等写法。

~~非法值返回 `None` → 416~~ —— **这条已经改了**：畸形 `Range`
（`bytes=`、`bytes=--`、非数字）现在**忽略这个头**，回 200 全量。
按 RFC 9110，畸形 `Range` 该被忽略而不是报错；回 416 会让播放器
直接放弃播放。只有「区间语法正确但越界」才轮到 416。

`_is_partial(header, span, size)` 决定状态码：没有头、不是 `bytes=` 开头、
或者区间等于整个文件 → 不是部分响应 → 200 且**不发** `Content-Range`。

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

状态码由 `DianGeJiError.http_status` 决定，**不是**按异常类硬编码：

```python
class DianGeJiError(Exception):
    http_status = 400        # 默认：用户能自己修
```

| 异常 | `http_status` | 前端看到 |
|---|---|---|
| `EmptyKeywordError` | 400 | 「空的点歌关键词」 |
| `TrackNotFoundError` / `NoPlayableTrackError` | 400 | 具体原因 |
| `DuplicateTrackError` | 400 | 「已经在队列里」 |
| `LibraryUnavailableError` | 500 | `INTERNAL_ERROR`，详情只进日志 |

```python
except DianGeJiError as exc:        # → self._domain_error(exc)
    if exc.http_status >= 500:
        log.error(..., exc_info=True)      # 完整栈写日志
        self._json({"ok": False, "error": INTERNAL_ERROR}, 500)
    else:
        self._json({"ok": False, "error": str(exc)}, exc.http_status)
except Exception:                   # 未知异常 → 500，文本绝不外泄
    log.exception(...)
    self._json({"ok": False, "error": INTERNAL_ERROR}, 500)
```

划分标准是**「谁能修」**：400 = 用户改一下关键词就行；500 = 服务端的问题，
回什么都帮不了用户，只会泄漏路径/token。

`EmptyKeywordError` 同时继承 `ValueError`，因为 `PlaybackService` 早期
抛的就是裸 `ValueError`，老调用方有 `except ValueError` 的不能断。

**⑥ 客户端断连（`CLIENT_GONE`）**

```python
CLIENT_GONE = (ConnectionError,)
```

原本写的是 `(BrokenPipeError, ConnectionResetError)` —— 漏掉了
`ConnectionAbortedError`。**它是这两者的兄弟，不是子类**：三个类
共同的父类是 `ConnectionError`。Windows 上客户端半路关连接报的正是
`ConnectionAbortedError [WinError 10053]`，于是日志被栈刷满。

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
