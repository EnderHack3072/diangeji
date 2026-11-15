# `live/` —— B 站弹幕层

`protocol.py` · `wbi.py` · `commands.py` · `client.py`

从「连上直播间的弹幕服务器」到「解析出一条点歌请求」，全在这一层。

---

## `protocol.py` —— 二进制协议

B 站弹幕用自定义二进制包，**16 字节头 + body**：

| 偏移 | 长度 | 内容 |
|---|---|---|
| 0 | 4 | 包总长 |
| 4 | 2 | 头长（固定 16） |
| 6 | 2 | 协议版本 |
| 8 | 4 | 操作码 |
| 12 | 4 | 序号 |

```python
def protover() -> int                      # 3 = brotli（装了才用），否则 0
def pack(opcode, body=b"", version=0, sequence=1) -> bytes
def auth_payload(room_id, token="", uid=0, version=None) -> bytes
def heartbeat_payload() -> bytes

class Packet:  length / header_len / version / opcode / sequence / body

def iter_packets(payload) -> Iterator[Packet]      # 拆包（一个 TCP 包里可能多个）
def decode_body(version, body) -> bytes | None     # 按版本解压
def iter_messages(payload) -> Iterator[dict]       # 一步到位拿 JSON
```

- **`iter_packets()` 必须处理粘包** —— 一次 recv 可能收到半个包，也可能收到
  三个包。它按头部的长度字段循环切。
- 版本 3 是 brotli 压缩。没装 `brotli` 时 `protover()` 返回 0，
  请求明文，功能不受影响（只是流量大点）。
- 版本 1（普通压缩）现在基本不用，但保留着兼容老房间。

---

## `wbi.py` —— WBI 签名

B 站 2023 年起给 `api.live.bilibili.com` 的接口加了 `w_rid`/`wts` 签名。

```python
class WBISigner:
    def mixin_key(cookie="", force=False) -> str        # 取 nav 接口的 key，缓存
    def sign(url, params, cookie="", timestamp=None) -> str   # 拼上 w_rid/wts
    def invalidate() -> None                            # 失效，下次重新取
```

- `mixin_key()` 缓存 `CACHE_TTL` 秒，避免每次都打一次 nav 接口。
- 取 key 的算法是把两张固定的字符表按 `img_key`/`sub_key` 重排，再取前 32 位。
- **`invalidate()` 是给 -352 错误码用的**：服务端说签名过期时，
  `live/client.py` 会 `invalidate()` 并重试一次。这条路径由
  `_danmu_info_once()` 承担，不要在看不懂的情况下删掉。

---

## `commands.py` —— 指令解析

```python
@dataclass
class Order:
    keyword: str
    raw_text: str                 # 原始弹幕，用于日志/排查
    ...

def parse_order(text) -> Order | None       # 是点歌就返回 Order，否则 None
def is_order(text) -> bool
```

- 触发词在 `constants.ORDER_PREFIXES`（「点歌」「点一首」「来首」…），
  改词只改那个常量。
- 会剥掉前缀、多余空白、全角空格。
- **纯函数**，不联网不碰状态，所以测试里直接喂字符串就行
  （`tests/test_live.py` 里有几十条真实弹幕样例）。

---

## `client.py` —— 长连接

```python
class DanmakuClient:
    def __init__(room_id, cookie="", on_order=None, on_log=None, cache_path=None)
    def start()                # 起 daemon 线程
    def stop(timeout=3.0)
    def alive() -> bool
```

内部流程（`_run()`，跑在 `danmaku` 线程上）：

```
1. _room_init()          拿真实房间号（短号 → 长号）
2. _danmu_info()         拿弹幕服务器地址 + token（要 WBI 签名）
   └── 命中 danmu_hosts.json 缓存且新鲜 → 跳过这步
3. 连 WebSocket，发 auth 包
4. 循环 _pump()：收包 → iter_messages → _handle_message
5. 心跳线程定时发 heartbeat
6. 断了就退避重连（3s 起，指数增长，封顶）
```

关键实现点：

- **`_danmu_info_once()` 单独拆出来**，因为 `-352`（签名过期）需要
  只重试这一步，而不是整个流程重来。
- **`_is_duplicate(uid, text)`** 挡重复弹幕：B 站同一条消息可能从多个分片
  推过来。用「uid + 文本 + 短时间窗」判重。
- **服务器地址缓存**写 `danmu_hosts.json`，走 `paths.atomic_write()`。
  缓存带 `ts` 和 `room`，房间换了或过期就重取。
- **`RateLimited` 异常**：被限流时不狂重连，退避时间拉长。
- `stop()` 会 join 线程，超时就放弃（daemon 线程不会阻止进程退出）。

### 一个回调，不要阻塞

`on_order` 最终进到 `app._handle_order()`，那里**只往 `queue.Queue` 里丢一条
就返回**，真正干活的是 `order-worker` 线程。原因：这个回调跑在弹幕线程上，
一阻塞就会错过后续弹幕、漏掉心跳，然后被服务端踢。

---

## 排错顺序

弹幕连不上时，按这个顺序看日志：

1. `_room_init` 失败 → 房间号错 / 直播间不存在
2. `_danmu_info` 返回 `-352` → WBI 签名问题，看 `wbi.py`
3. 连上了但收不到消息 → 协议版本 / brotli 没装
4. 收到消息但没进队列 → `parse_order()` 没匹配上，看触发词
5. 进了队列但没播 → 不是这层的问题，去看 `services/playback.py`
