# 可靠 Webhook 投递子系统（Python / FastAPI / SQLite）

一个本地可完整运行、不依赖任何外部服务的可靠 Webhook 投递系统。
业务侧提交事件后立即返回，实际投递在后台完成；同一事件对同一订阅方
**最多成功送达一次**，失败按指数退避重试，终态原因可区分；订阅方可
注册、启用、停用，非法配置在入口被拒绝。

## 能力一览

- **异步投递**：事件只做本地 SQLite 写入（毫秒级）即返回，HTTP 投递由
  后台 worker 完成；每条投递是独立 asyncio 任务，某个慢订阅方不会拖
  住其它订阅方或后续事件。
- **幂等去重**：`events.event_id` 与 `deliveries(event_id, subscription_id)`
  均有数据库唯一约束；重复提交、并发（多线程/多 worker）触发都只会产生
  一条投递记录。
- **带签名投递**：每次请求带 `X-Webhook-Timestamp` 和
  `X-Webhook-Signature`（HMAC-SHA256），规则见 [docs/protocol.md](docs/protocol.md)。
- **退避重试与终态**：非 2xx / 超时 / 连接失败按指数退避重试，超限进入
  终态；`重试耗尽`、`签名校验失败`、`订阅方不可达` 分别是不同状态，
  还能查到每一次尝试的明细（状态码、原因、耗时、响应摘录）。
- **订阅方管理**：注册 / 启用 / 停用 / 按事件类型订阅；停用后不再投递，
  待发记录进入 `skipped` 终态，重新启用后新事件恢复送达。
- **严格配置校验**：非 http(s) URL、过短/弱密钥、空事件类型等一律 422 拒绝。
- **本地模拟接收端**：内置可验签、可模拟慢响应/5xx/偶发失败/密钥不一致的
  接收服务，配合本地配置完成端到端验证。

## 目录结构

```
webhook/
  config.py        # 本地设置加载
  models.py        # 状态/原因枚举与领域模型
  errors.py        # 业务错误类型
  validation.py    # URL / secret / 事件类型校验
  signing.py       # HMAC-SHA256 签名与时间戳新鲜度
  db.py            # SQLite 连接（线程安全封装）与 outbox 表结构
  repositories.py  # 订阅方/事件/投递/尝试记录数据访问（含幂等插入、原子抢占）
  delivery.py      # httpx 投递客户端，归类 timeout/connection/non_2xx/拒签
  retry.py         # 指数退避
  worker.py        # 后台 worker：领取、发送、退避、终态归类
  service.py       # 应用服务：注册、启停、提交事件、查询
  api.py           # FastAPI 路由
  app.py           # 应用装配（lifespan 启停 worker）
  receiver.py      # 本地模拟接收端
  cli.py           # server / receiver / seed / demo
config/
  settings.json        # 本地运行设置（轮询间隔、超时、重试参数）
  subscriptions.json   # 本地模拟订阅方（地址/密钥均为本机值）
tests/                  # 55 个用例（单测 + 真实 HTTP 端到端）
docs/protocol.md        # 投递协议 / 签名 / 重试规则
main.py                 # uvicorn 入口
```

## 快速开始

依赖使用 [uv](https://docs.astral.sh/uv/) 管理：

```bash
uv sync --group dev
```

### 方式一：一条龙 demo（推荐先跑）

```bash
# 终端 A：启动本地模拟接收端（127.0.0.1:9100）
uv run python -m webhook.cli receiver

# 终端 B：注册订阅方并启动投递服务（127.0.0.1:8080）
uv run python -m webhook.cli seed
uv run python -m webhook.cli server
```

`config/subscriptions.json` 内置 5 个订阅方，分别指向接收端的
`/hook/ok`、`/hook/flaky`、`/hook/fail`、`/hook/slow`、`/hook/badsecret`。

提交一个事件（会同时投递给匹配的订阅方）：

```bash
curl -s -X POST http://127.0.0.1:8080/api/v1/events \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"evt-1","event_type":"order.created","payload":{"order_id":"A-1"}}'
# 首次提交 -> 202；用同样 event_id 再提交 -> 200(deduped=true)，不会重复投递
```

查询投递结果：

```bash
curl -s http://127.0.0.1:8080/api/v1/events/evt-1 | python -m json.tool
# 查看某次投递的每次尝试明细（状态码/原因/耗时）
curl -s http://127.0.0.1:8080/api/v1/deliveries/1 | python -m json.tool
# 查看接收端实际成功收到的请求
curl -s http://127.0.0.1:9100/received | python -m json.tool
```

不同路径会观察到不同终态：

| 订阅方路径 | 预期结果 |
| --- | --- |
| `/hook/ok` | `succeeded`，1 次成功 |
| `/hook/flaky` | 前两次 503，第三次 `succeeded`（重试生效） |
| `/hook/fail` | 持续 503，最终 `failed_retries_exhausted` |
| `/hook/slow` | 超过 HTTP 超时，最终 `failed_subscriber_unreachable`（timeout） |
| `/hook/badsecret` | 401 拒签，`failed_signature_rejected`，仅尝试 1 次 |

### 方式二：注册你自己的订阅方

```bash
curl -s -X POST http://127.0.0.1:8080/api/v1/subscriptions \
  -H 'Content-Type: application/json' -d '{
    "name": "my-local-consumer",
    "url": "http://127.0.0.1:9100/hook/ok",
    "secret": "receiver-local-secret-0123456789",
    "event_types": ["order.created", "order.paid"]
  }'
```

非法配置示例（均返回 422 并给出原因）：

```bash
# URL 协议不支持
curl -X POST .../subscriptions -d '{"name":"x","url":"ftp://h/y",
  "secret":"0123456789abcdef","event_types":["t"]}'
# 密钥过短（要求至少 16 字节）
curl -X POST .../subscriptions -d '{"name":"x","url":"http://127.0.0.1/y",
  "secret":"short","event_types":["t"]}'
# 事件类型为空
curl -X POST .../subscriptions -d '{"name":"x","url":"http://127.0.0.1/y",
  "secret":"0123456789abcdef","event_types":[]}'
```

### 启用 / 停用

```bash
# 停用（:id 用注册接口返回或 /subscriptions 列表里的 id）
curl -X POST http://127.0.0.1:8080/api/v1/subscriptions/1/enabled \
  -H 'Content-Type: application/json' -d '{"enabled": false}'
# 重新启用
curl -X POST http://127.0.0.1:8080/api/v1/subscriptions/1/enabled \
  -H 'Content-Type: application/json' -d '{"enabled": true}'
```

停用后新事件不再匹配该订阅方，已存在但未成功的待发记录直接进入
`skipped_subscriber_disabled`；重新启用后新产生的事件恢复正常投递。

## HTTP API

| 方法与路径 | 说明 |
| --- | --- |
| `POST /api/v1/subscriptions` | 注册订阅方（201 / 422） |
| `GET  /api/v1/subscriptions` | 列出全部订阅方 |
| `GET  /api/v1/subscriptions/{id}` | 查看单个订阅方 |
| `POST /api/v1/subscriptions/{id}/enabled` | `{"enabled": true/false}` 启用/停用 |
| `POST /api/v1/events` | 提交事件；首次 202，重复 `event_id` 200 且 `deduped=true` |
| `GET  /api/v1/events/{event_id}` | 查事件下每个订阅方的投递状态 |
| `GET  /api/v1/deliveries/{id}` | 查单次投递的全部尝试明细 |
| `GET  /health` | 健康检查 |

投递状态取值：`pending`、`in_flight`、`retrying`、`succeeded`、
`failed_retries_exhausted`、`failed_signature_rejected`、
`failed_subscriber_unreachable`、`skipped_subscriber_disabled`。

## 运行测试

```bash
uv run pytest -v
```

测试分几层：

- `test_signing / test_retry / test_validation`：签名、退避、参数校验纯逻辑；
- `test_db`：建表幂等与唯一约束；
- `test_worker`：用假投递器覆盖重试到终态、不可达、拒签即终态、失败后成功；
- `test_claim`：两个独立 worker 并发抢占，证明不会重复发送；
- `test_api`：注册/非法配置拒绝/启停/提交去重/查询的 HTTP 行为；
- `test_e2e_delivery / test_e2e_failures`：在本机真实 TCP 上启动模拟
  接收端，覆盖**重复提交、20 路并发重复事件、慢订阅方不阻塞他人、
  flaky 重试成功、重试耗尽、连接拒绝不可达、密钥不一致拒签、停用与
  恢复、停用时跳过待发记录**。

`-v` 输出逐条用例名与 PASSED/FAILED，即为覆盖的业务场景与通过情况清单。

## 配置

`config/settings.json`（仅本地值）：

```json
{
  "database_path": "data/webhook.db",
  "poll_interval": 0.1,
  "http_timeout": 5.0,
  "max_attempts": 5,
  "base_delay": 1.0,
  "max_delay": 60.0,
  "timestamp_skew_seconds": 300
}
```

所有订阅地址均指向 `127.0.0.1`，密钥写在本地
`config/subscriptions.json` 中，无任何外部服务或真实凭据。

## 可靠性设计要点

- **Outbox + 唯一约束**：提交事件与创建投递记录是本地事务式写入；
  唯一约束是去重的最终防线，不依赖"先查后插"。
- **原子领取**：worker 用条件更新 `status IN ('pending','retrying')
  -> 'in_flight'` 抢占记录；进程崩溃残留的 `in_flight` 会在启动时退回
  `pending`。
- **并发安全**：SQLite 连接用可重入锁串行化所有本地读写（均为毫秒级），
  HTTP I/O 在锁外异步进行，互不影响吞吐。
- **故障隔离**：每条投递一个 asyncio Task，慢/挂死的订阅方只占用自己的
  任务与自己的退避计时器。
