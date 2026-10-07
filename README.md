# Webhook 投递子系统（Outbox 模式）

一个**本地可完整运行、可验证**的可靠 Webhook 投递服务：业务侧提交事件后立即返回，
后台 worker 异步向订阅方投递带 HMAC 签名的 HTTP 请求，支持退避重试、去重、
订阅方启停与投递结果查询。所有组件（API、worker、SQLite、模拟接收端）都在本地运行，
**不依赖任何外部服务或真实凭据**。

## 1. 能力总览

| 需求 | 实现方式 |
| --- | --- |
| 提交即返回、异步投递 | 事件与投递先落本地 SQLite（Outbox），后台线程池轮询发送 |
| 同一事件对同一订阅方最多成功送达一次 | `deliveries(event_id, subscriber_id)` 唯一约束 + 投递认领加锁；成功即终态 |
| 重复提交 / 并发触发不重复投递 | `events.event_id` 唯一约束，重复提交返回 `deduplicated=true`，不新建投递 |
| 慢订阅方不拖累他人 | 每条投递在线程池独立执行；HTTP 有超时；认领按投递粒度 |
| 签名 + 时间戳 | HMAC-SHA256，见 [§4](#4-签名协议) |
| 非 2xx/超时/连不上退避重试 | 指数退避，达到上限进入终态，见 [§5](#5-投递结果分类与重试规则) |
| 失败原因可区分 | `retries_exhausted` / `unreachable` / `signature_invalid` / `timestamp_skew` / `permanent_reject` |
| 投递结果可查询 | 每次投递、每次尝试（状态码、错误类别、接收方错误码、详情、耗时）均可查 |
| 订阅方管理 | 注册 / 启用 / 停用 / 按事件类型订阅；非法 URL、密钥等明确以 422 拒绝 |

## 2. 架构与目录

```
main.py                  启动入口（FastAPI + 后台 worker）
app/
  api.py                 HTTP 接口与应用装配（worker 在应用生命周期内启停）
  service.py             应用服务：订阅方管理、事件提交与投递扇出
  worker.py              后台投递工作线程池（轮询 outbox、认领、分派）
  deliveryexecutor.py    单次投递执行、响应分类、退避排程/终态
  transport.py           标准库 HTTP 传输（区分超时/连接失败）
  signing.py             HMAC-SHA256 签名与校验
  storage.py             Outbox 存储（事件/订阅方/投递/尝试，幂等与原子认领）
  db.py                  SQLite 连接与建表
  validation.py          URL / 密钥 / 事件类型 / 事件校验
  config.py              本地配置（环境变量，全部有默认值）
  models.py              领域模型与枚举
tools/
  mock_receiver.py       本地模拟接收端（验签 + 多种故障行为）
scripts/demo.sh          一键端到端演示
tests/                   27 个用例（幂等/并发/慢订阅方/重试终态/启停/校验/API）
config/subscribers.example.json
```

### 数据流

```
业务 ─POST /events─▶ service.submit_event
                        │  (单事务: 写 events + 按启用订阅方 fan-out 写 deliveries)
                        ▼
                 SQLite Outbox 表
                        ▲
        worker 轮询到期 deliveries（BEGIN IMMEDIATE 原子认领加锁）
                        ▼
        线程池：executor 构造签名请求 → HTTP POST → 记录 attempt
                        │
        2xx → succeeded；可重试错误 → 退避后排程；明确终态错误 → failed(原因)
```

## 3. HTTP 接口

基础地址默认 `http://127.0.0.1:8080`（演示脚本用 9200）。

### 订阅方管理

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /subscribers` | 注册订阅方（校验失败返回 422） |
| `GET  /subscribers` | 列出全部订阅方（含停用） |
| `POST /subscribers/{id}/disable` | 停用（不存在返回 404） |
| `POST /subscribers/{id}/enable` | 重新启用 |

注册请求体：

```json
{
  "name": "orders-service",
  "url": "http://127.0.0.1:9100/hook/ok",
  "secret": "local-secret-good",
  "event_types": ["order.created", "order.paid"]
}
```

校验规则（不通过即以 422 拒绝，绝不带病入库）：

- `url`：必填，只允许 `http`/`https`，必须有主机名；不允许在 URL 里携带用户名/密码；长度 ≤ 2048。
- `secret`：必填，长度 8–256，不能含空白字符。
- `event_types`：非空字符串数组，不允许重复、空串。
- `name`：必填。

### 事件提交与查询

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /events` | 提交事件，**立即返回 202**；响应给出本次新建的投递；重复提交返回 `deduplicated: true` 且投递列表为空 |
| `GET  /events/{event_id}` | 查询事件及其投递状态 |
| `GET  /deliveries?event_id=&subscriber_id=` | 按事件/订阅方过滤投递 |
| `GET  /deliveries/{id}` | 投递详情 + 每一次尝试 `attempt_history` |
| `GET  /meta/statuses` | 状态与失败原因枚举 |
| `GET  /health` | 健康检查与 worker 运行状态 |

提交请求体：

```json
{ "event_id": "evt-001", "event_type": "order.created", "payload": {"order": "A-1001"} }
```

投递状态：`pending`（待投/退避等待中）、`succeeded`、`failed`。

## 4. 签名协议

投递请求为 `POST`，`Content-Type: application/json`，body 为事件 payload 的原始 JSON 字节，
并携带三个头：

| 头部 | 内容 |
| --- | --- |
| `X-Webhook-Event` | 事件业务 ID（`event_id`） |
| `X-Webhook-Timestamp` | 发送方 Unix **秒级**时间戳 |
| `X-Webhook-Signature` | `sha256=<HMAC-SHA256 十六进制摘要>` |

签名原文（注意是「时间戳.事件ID.原始body」用 `.` 拼接的字符串）：

```
signed_payload = "{timestamp}.{event_id}.{body}"
signature_hex  = HMAC_SHA256(secret, signed_payload)
```

接收方校验步骤（`tools/mock_receiver.py` 即参考实现）：

1. 解析 `X-Webhook-Timestamp`，要求与当前时间差绝对值 ≤ 300 秒
   （由 `WEBHOOK_TOLERANCE` / 接收端 `--tolerance` 配置），超窗返回
   `401 {"error_code":"timestamp_skew"}`。
2. 用订阅方 secret 按同样规则计算 HMAC，与 `X-Webhook-Signature`
   做**恒定时间比较**（`hmac.compare_digest`），不匹配返回
   `401 {"error_code":"signature_invalid"}`。
3. 通过后返回任意 2xx。

固定向量示例：

```
secret      = "abc12345"
timestamp   = 1700000000
event_id    = "evt-1"
body        = "{}"
signed      = b"1700000000.evt-1.{}"
signature   = "sha256=" + hmac_sha256(b"abc12345", signed).hexdigest()
```

接收方若需要区分签名/时间戳错误，必须在 **401** 响应体中带
`{"error_code": "signature_invalid"}` 或 `{"error_code": "timestamp_skew"}`。

## 5. 投递结果分类与重试规则

| 接收方表现 | 行为 | 终态 `failure_reason` |
| --- | --- | --- |
| 2xx | 成功，结束 | —（`none`） |
| 5xx / 408 / 425 / 429 | 指数退避重试，次数用尽后终态 | `retries_exhausted` |
| 连接被拒 / 不可达 | 指数退避重试，次数用尽后终态 | `unreachable` |
| 请求超时 | 指数退避重试，次数用尽后终态 | `retries_exhausted`（每次尝试 `error_kind=timeout` 可查） |
| 401 + `error_code=signature_invalid` | **不重试**，立即终态 | `signature_invalid` |
| 401 + `error_code=timestamp_skew` | **不重试**，立即终态 | `timestamp_skew` |
| 其它 4xx（如 404） | **不重试**，立即终态 | `permanent_reject` |
| 订阅方不存在/配置损坏 | 立即终态 | `bad_subscriber` |

指数退避（无抖动，行为确定）：

```
第 n 次失败后的等待 = min(base_backoff * 2^(n-1), max_backoff)
默认 base_backoff = 0.2s, max_backoff = 30s
```

- 默认最多尝试 **5 次**（即首次 + 4 次重试），可由 `WEBHOOK_MAX_ATTEMPTS` 调整。
- 每次尝试都写入 `delivery_attempts`：尝试序号、开始/结束时间、结果、HTTP 状态码、
  错误类别（`timeout`/`connection`/`http_error`）、接收方错误码、详情。
- 投递被 worker 认领后会持有短期锁（默认 30s，`WEBHOOK_LOCK_SECONDS`），
  worker 每轮会回收过期的遗留锁（崩溃恢复）。
- **成功投递是终态**，不会重复发送；重复事件在存储层就不会创建第二条投递。

配置项（均有默认值，可用环境变量覆盖）：

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `WEBHOOK_DB_PATH` | `data/webhooks.db` | SQLite 路径 |
| `WEBHOOK_MAX_ATTEMPTS` | `5` | 最大尝试次数 |
| `WEBHOOK_BASE_BACKOFF` | `0.2` | 退避基数（秒） |
| `WEBHOOK_MAX_BACKOFF` | `30` | 退避上限（秒） |
| `WEBHOOK_TIMEOUT` | `5` | 单次 HTTP 超时（秒） |
| `WEBHOOK_LOCK_SECONDS` | `30` | 投递认领锁时长 |
| `WEBHOOK_POLL_INTERVAL` | `0.1` | worker 轮询间隔 |
| `WEBHOOK_TOLERANCE` | `300` | 时间戳容忍窗口（秒） |
| `WEBHOOK_WORKER_THREADS` | `8` | 投递线程池大小 |

## 6. 本地验证

需要 Python 3.14+ 与 [uv](https://docs.astral.sh/uv/)。依赖安装由 uv 自动完成。

### 6.1 运行单元/集成测试

```bash
uv sync            # 安装含 pytest 的依赖
uv run pytest      # 27 个用例，约 10s
```

测试结尾会打印「业务场景覆盖结果」清单，逐场景显示 PASS。覆盖：

- **签名协议**：合法签名通过、body/事件/时间戳/密钥被篡改时拒绝、固定向量、空签名拒绝。
- **幂等去重**：重复提交只产生一条投递；20 线程并发提交同一事件，接收端只收到一次。
- **慢订阅方隔离**：1.5s 慢订阅方处理期间，快订阅方与后续事件照常秒级送达。
- **重试与终态**：500 退避重试到上限（校验退避间隔与尝试明细）、端口不可达→`unreachable`、
  401 签名失败→`signature_invalid`、401 时间戳失败→`timestamp_skew`、404→`permanent_reject`，
  以及四类原因同批可区分。
- **订阅方生命周期**：停用即不投、恢复后只投新事件（不补发）、非法 URL/密钥/事件类型拒绝、404。
- **HTTP API**：注册/启停、提交去重、投递与尝试明细查询、错误码。

### 6.2 一键端到端演示

```bash
./scripts/demo.sh
```

脚本会在本地启动模拟接收端与 API/worker，自动注册订阅方、提交事件（含重复提交）、
等待重试、查询终态、停用/恢复订阅方，最后清理进程。预期可看到：

```
首次提交 deduplicated= False 投递数= 3
重复提交 deduplicated= True 投递数= 0
  dlv_xxx status=failed    attempts=3 reason=retries_exhausted
  dlv_xxx status=succeeded attempts=1 reason=none
  dlv_xxx status=failed    attempts=1 reason=signature_invalid
```

### 6.3 手动分两步启动

```bash
# 终端 A：模拟接收端（/hook/ok /hook/500 /hook/badsig /hook/oldskew /hook/slow /hook/404 /hook/429）
uv run python -m tools.mock_receiver --port 9100 \
  --secrets ok=local-secret-good,slow=local-secret-slow

# 终端 B：API + worker
uv run uvicorn main:app --host 127.0.0.1 --port 8080

# 终端 C：操作
curl -s -X POST localhost:8080/subscribers -H 'Content-Type: application/json' -d '{
  "name":"local","url":"http://127.0.0.1:9100/hook/ok",
  "secret":"local-secret-good","event_types":["order.created"]}'
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' -d '{
  "event_id":"evt-1","event_type":"order.created","payload":{"hello":"world"}}'
curl -s localhost:8080/deliveries
```

交互式 API 文档：启动后访问 `http://127.0.0.1:8080/docs`。

## 7. 设计说明与边界

- **单机可靠，不跨进程重复发送**：Outbox + 唯一约束保证不重复；投递认领用
  `BEGIN IMMEDIATE` 串行化写事务并配合进程内锁。多实例部署时建议只让一个实例跑 worker，
  或改用中心化队列；本项目定位为本地单机方案。
- **去重以 `event_id` 为业务幂等键**：调用方需要为同一件事使用稳定的 event_id。
- **失败不丢事件**：成功前投递始终留在 `deliveries` 中并按退避重新排队；进程重启后
  worker 会回收遗留锁继续投递。
- **送达语义**：在上述约束下提供「至多一次成功送达」。极端情况下（服务端已处理成功、
  响应在网络中丢失）可能重试，因此接收方自身也建议以 `X-Webhook-Event` 做幂等。
