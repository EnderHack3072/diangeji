# `auth/` —— 登录与凭证

`store.py` · `qrcode_login.py` · `browser.py`

B 站和 QQ 音乐都要 cookie。三条获取路径，一个统一的存储。

---

## `store.py` —— cookie 存储

```python
def normalize_cookie(value: str | Mapping[str, str]) -> str
def split_pairs(cookie: str) -> list[tuple[str, str]]

class CookieStore:
    def __init__(directory: Path | None = None)
    def path(filename) -> Path
    def read(filename) -> str
    def has(filename) -> bool
    def value(filename, key) -> str
    def write(filename, cookie) -> bool        # 空内容拒绝写入
    def merge(filename, extra) -> bool          # 增量合并
    def as_mapping(cookie) -> dict
    def clear(filename)
```

约定：

- 文件名常量在模块顶部：`BILI_COOKIE` / `QQ_COOKIE`。
- **空内容一律拒绝写入** —— 一次失败的网络请求不该把好文件清空。
  这是刻意的保护，`write()` 返回 `False` 而不是抛异常。
- 原子写走 `paths.atomic_write()`，失败时清掉临时文件。
- `merge()` 是「缺什么补什么」，不覆盖已有键 —— 扫码登录只拿得到部分
  字段时用这个。
- cookie 是**明文**存的。发给别人时记得排除这两个文件。

---

## `qrcode_login.py` —— 扫码登录

```python
class LoginResult(str, Enum):
    SUCCESS / EXPIRED / SCANNED / CONFIRMED / ...

@dataclass
class QRPayload:
    url: str              # 二维码内容
    key: str              # 轮询用的 key
    png: bytes | None     # 服务端直接给的图（B 站有）
    base64: str | None

class BiliQRLogin:
    def start() -> QRPayload
    def poll() -> tuple[LoginResult, str]      # (结果, cookie)

class QQMusicQRLogin:
    def start() -> QRPayload
    def poll() -> tuple[LoginResult, str]
```

- **二维码优先用服务端给的 PNG**；没有才用本地 `qrcode` 生成。
  所以 `qrcode` 是可选依赖，缺了也能登录（只要能拿到图）。
- `png_matrix()` 把 PNG 解成 0/1 矩阵，`_trim_and_pad()` 裁掉白边再补静区 ——
  这样界面能按任意尺寸重绘，不会糊。
- `login_state` 是个状态机：`WAITING(86101) → SCANNED(86090) → CONFIRMED(0)`，
  `EXPIRED(86038)` 终止。**轮询要有超时**，别无限转。
- QQ 音乐的流程多一步 `_follow()`：登录成功后还得跳一次拿到真正的音乐 cookie。

---

## `browser.py` —— CDP 抓 cookie

有些登录态（尤其 QQ 音乐）拿不到扫码接口，只能开真浏览器：
启动 Edge 的远程调试端口，操作完直接读 cookie。

```python
def find_edge() -> str | None
def clean_env(environ=None) -> dict          # 清掉会干扰的代理变量
def clean_cwd() -> str                       # 给一个干净的工作目录

class CDPClient:
    def __init__(port, timeout=2.0)
    def alive() -> bool
    def wait_ready(attempts=40, interval=0.6) -> bool
    def tabs() / open_tab(url) / find_page(host) / first_page()
    def call(method, params=None, ws_url=None)
    def navigate(url, ws_url=None)
    def cookies(urls, wanted) -> str           # 只取想要的键

class BrowserLogin:
    def __init__(profile_dir, data_dir, edge_path=None)
    def available() -> bool
    def launch(url, port, reset=False)
    def login_flow(url, port, wanted, ready_urls=None,
                   timeout=180.0, poll_interval=2.0) -> str
    def kill()
```

要点：

- 用**独立的 `_edgeprofile/` 目录**，不碰用户自己的浏览器配置。
- `clean_env()` 清 `HTTP_PROXY` 之类 —— 有的系统代理会让 CDP 端口连不上。
- `login_flow()` 是「开浏览器 → 等用户扫/登 → 轮询 cookie → 拿到就返回」，
  **有总超时**（默认 180s），不会卡死。
- CDP 走 WebSocket，用 `utils/ws.py` 那个客户端。
- 端口被占时 `wait_ready()` 会失败，`available()` 返回 `False`，
  上层退回扫码登录。

> **不要自动 kill 用户的浏览器进程。** `BrowserLogin.kill()` 只杀自己
> 用独立 profile 启起来的那个实例，靠 `--user-data-dir` 匹配，不是按
> 进程名通杀。
