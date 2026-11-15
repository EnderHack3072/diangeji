# `utils/` —— 通用工具

`text.py` · `timeutil.py` · `webhttp.py` · `ws.py`

**无业务依赖。** 这一层不 import 任何 `core`/`sources`/`live`，
反过来谁都可以用。

---

## `text.py` —— 文本处理

```python
def strip_order_prefix(text) -> str       # 剥掉「点歌」等前缀
def ellipsize(text, limit=40, suffix="…") -> str
def clean_filename(name, fallback="untitled") -> str
def escape_js_string(text) -> str         # 安全地嵌进 <script>
def humanize_seconds(seconds) -> str      # "3:07"
```

- `clean_filename()` 去掉 Windows 非法字符（`<>:"/\|?*`）和保留名（`CON`、`PRN`…）。
- `escape_js_string()` 必须处理 `</script>`、`<!--`、`</` ——
  内联 JSON 到 `<script>` 里不做这个就会提前闭合标签。
- 全是纯函数，无副作用，好测。

---

## `timeutil.py`

```python
def now_ts() -> int              # Unix 秒
def to_clock(ts) -> str          # "21:39:50"
```

时间格式化集中在这里。**不要在别处再写 `time.strftime`** ——
格式统一了日志才好比对。

---

## `webhttp.py` —— HTTP 客户端

```python
class HttpError(RuntimeError): ...      # 带 status / body

def request(url, *, data=None, headers=None, timeout=15, method=None) -> bytes
def get_json(url, *, headers=None, timeout=15) -> Any
def post_json(url, payload, *, headers=None, timeout=15) -> Any
def with_query(url, params) -> str
def host_of(url) -> str
```

要点：

- 统一加 UA。不少接口（尤其 B 站）没 UA 直接 412。
- `_loads()` 按 `utf-8` → `gbk` 顺序试编码。QQ 音乐某些接口返回 GBK。
- 非 2xx 抛 `HttpError`，带状态码和响应体片段，方便日志定位。
- `Timeout` 有默认值，**不允许无超时调用** —— 网络卡住不该挂死线程。
- 没有 `requests` 依赖，全用 `urllib`。

---

## `ws.py` —— WebSocket 客户端

```python
class WebSocketError(ConnectionError): ...

class WSClient:
    def connect(url, headers=None) -> None
    def send(data: bytes) -> None
    def recv() -> bytes
    def close(graceful=True) -> None
```

为什么要自己写（不上 `websocket-client`）：**零依赖**是项目的硬要求，
而且只用到最基础的部分（文本/二进制帧、ping/pong、close），
不需要扩展协议。

关键实现：

- 握手自己拼 `Sec-WebSocket-Key`，校验 `Sec-WebSocket-Accept`。
- 支持分片帧的重组（弹幕量大时会出现）。
- **`connect()` 失败会自己清理**：

```python
def connect(self, url, headers=None):
    try:
        ...握手、建连...
    except BaseException:
        self.close(graceful=False)   # 别留半开的 socket
        raise
```

- **`close(graceful=False)` 不发 CLOSE 帧**。连接已经坏了的时候发 CLOSE
  只会再抛一次异常，盖掉真正的错误。

- 收发用阻塞 socket + 超时，配合调用方线程模型（一个线程收、需要时另一个发）。

---

## 加新工具的规矩

1. 只依赖标准库和 `logging_setup`，**不要** import 业务模块
2. 纯逻辑，不读配置文件、不碰 `data_dir()`
3. 放进来之前先问：这真的通用吗？只给一个模块用的工具应该留在那个模块里
