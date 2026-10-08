# Webhook 投递协议、签名与重试规则

本文档面向**外部订阅方（接收端）开发者**，说明本系统如何向你的回调地址
投递事件，以及你应当如何校验签名、处理重试。

## 1. 投递请求

* 方法：`POST`
* `Content-Type: application/json; charset=utf-8`
* Body（紧凑 JSON，UTF-8）：

```json
{
  "event_id": "evt-20261008-0001",
  "event_type": "order.created",
  "payload": { "order_id": "A-1", "total": 99 }
}
```

* 请求头：

| 头 | 含义 |
| --- | --- |
| `X-Webhook-Id` | 业务事件幂等 ID。同一 `event_id` 对同一订阅方**最多成功送达一次** |
| `X-Webhook-Event` | 事件类型，如 `order.created` |
| `X-Webhook-Timestamp` | 签名时间戳，Unix 秒（UTC 整数） |
| `X-Webhook-Signature` | 签名，格式 `sha256=<十六进制 HMAC>` |
| `User-Agent` | `solo-webhook-delivery/<version>` |

## 2. 签名规则（HMAC-SHA256）

被签名的内容（注意中间是英文句点 `.`，body 为**原始字节**）：

```
<X-Webhook-Timestamp> + "." + <raw request body bytes>
```

使用订阅方注册时各自的 `secret` 做 HMAC-SHA256，最终头为：

```
X-Webhook-Signature: sha256=<hexdigest>
```

### 2.1 接收端校验步骤

1. 读取 `X-Webhook-Timestamp`，与当前时间比较；偏差超过约定窗口
   （默认 **300 秒**）即拒绝，以防重放。
2. 读取**未经任何重新序列化的原始请求体**，拼接
   `timestamp + "." + raw_body`。
3. 用自己的 secret 计算 HMAC-SHA256，与 `X-Webhook-Signature`
   去掉 `sha256=` 前缀后的值做**常数时间**比较。
4. 一致且时间戳新鲜才处理；否则返回 `401` 并带头
   `X-Webhook-Error: invalid_signature`（或 `invalid_timestamp`）。

> 必须用原始 body 字节计算。先把 JSON 反序列化再 `json.dumps` 会因为
> 空格/键序/Unicode 转义差异导致签名不一致。

### 2.2 Python 参考实现

```python
import hashlib, hmac, time

def verify(secret: str, timestamp: str, raw_body: bytes, signature: str,
           max_skew: int = 300) -> bool:
    if abs(time.time() - int(timestamp)) > max_skew:
        return False
    expected = hmac.new(
        secret.encode(),
        timestamp.encode() + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    supplied = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, supplied)
```

### 2.3 命令行验证示例

```bash
# 假设 ts=1728380000，body 为文件 body.json 的原始内容
printf '%s.%s' "1728380000" "$(cat body.json)" \
  | openssl dgst -sha256 -hmac "$SECRET"
```

## 3. 应答约定与重试

### 3.1 成功

返回任意 **2xx** 即视为送达成功，流程结束，不再重试。

### 3.2 签名/时间戳校验失败

返回 `401`，并带响应头：

```
X-Webhook-Error: invalid_signature   # 或 invalid_timestamp
```

投递端把它识别为**配置错误**（双方密钥不一致/时间不同步），
**不会重试**，直接进入终态 `failed_signature_rejected`。
普通业务 4xx/5xx 不应使用该头。

### 3.3 可重试失败与退避

下列情况按指数退避重试：

* 非 2xx 响应（4xx/5xx，且不是 3.2 的签名拒绝）——原因记为 `non_2xx`；
* 请求超时（默认单次 5 秒）——`timeout`；
* 连接失败（拒绝连接/DNS 失败/连接重置）——`connection_error`。

退避公式（第 `n` 次失败后，`n` 从 1 开始）：

```
delay(n) = min(max_delay, base_delay * 2 ** (n - 1))
```

默认 `base_delay = 1s`、`max_delay = 60s`、最多尝试 `max_attempts = 5` 次，
即大致间隔 1s、2s、4s、8s（封顶 60s）。

### 3.4 终态分类（三种失败原因彼此可区分）

| 终态 | 含义 | 典型 `last_failure_reason` |
| --- | --- | --- |
| `succeeded` | 2xx 成功 | `none` |
| `failed_retries_exhausted` | 重试上限内一直非 2xx（对端可达） | `non_2xx` |
| `failed_subscriber_unreachable` | 所有尝试均超时/连接失败，一次响应都没拿到 | `timeout` / `connection_error` |
| `failed_signature_rejected` | 对端 401 + `X-Webhook-Error` 明确拒签，不重试 | `signature_rejected` |
| `skipped_subscriber_disabled` | 订阅方被停用，待发记录直接跳过 | `unreachable` |

## 4. 幂等与"最多一次"语义

* 投递由数据库 outbox 驱动：事件和 `(事件, 订阅方)` 投递记录都有
  唯一约束，重复提交 / 并发提交只产生一条投递记录。
* 投递记录由后台 worker 用"状态条件抢占"（`pending/retrying -> in_flight`）
  领取，多个 worker 不会重复发送同一条。
* 因此同一 `event_id` 对同一订阅方**最多成功送达一次**；失败重试期间
  可能出现多次 HTTP 请求（at-least-once 传输尝试 + 成功只认定一次）。
  接收端若需要对"重试到达"彻底免疫，可用 `X-Webhook-Id` 自行去重。

## 5. 投递记录状态机

```
pending ──claim──▶ in_flight ──2xx────────────────────▶ succeeded
   ▲                  │
   │(启动恢复)         ├──401+X-Webhook-Error──────────▶ failed_signature_rejected
   │                  ├──超时/连接失败/非2xx 且未到上限─▶ retrying
   │                  │                                      │
   └──────────────────┴────────────(到点 claim)──────────────┘
                      │
                      └──达到 max_attempts──▶ failed_retries_exhausted
                                             或 failed_subscriber_unreachable

停用订阅方时：pending / retrying / in_flight ──▶ skipped_subscriber_disabled
```
