#!/usr/bin/env bash
# 本地端到端演示：模拟接收端 + Webhook API/worker，不依赖任何外部服务。
#
# 演示内容：
#   1. 注册 4 个订阅方：正常 / 始终 500 / 验签失败 / 停用后恢复
#   2. 提交事件（含一次重复提交），观察幂等
#   3. 等待后台 worker 异步投递、退避重试到终态
#   4. 查询投递状态、每次尝试明细、可区分的失败原因
#   5. 停用期间不投递，重新启用后新事件继续送达
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

API_PORT="${API_PORT:-9200}"
RCV_PORT="${RCV_PORT:-9201}"
API_BASE="http://127.0.0.1:${API_PORT}"
RCV_BASE="http://127.0.0.1:${RCV_PORT}"
WORKDIR="$(mktemp -d)"
trap 'echo; echo "== 清理 =="; kill "$API_PID" "$RCV_PID" 2>/dev/null || true; rm -rf "$WORKDIR"' EXIT

echo "== 1. 启动本地模拟接收端 (:${RCV_PORT}) =="
uv run python -m tools.mock_receiver --port "$RCV_PORT" \
  --secrets ok=local-secret-good,slow=local-secret-slow \
  >"$WORKDIR/receiver.log" 2>&1 &
RCV_PID=$!

echo "== 2. 启动 Webhook API + 后台投递 worker (:${API_PORT}) =="
WEBHOOK_DB_PATH="$WORKDIR/demo.db" \
WEBHOOK_MAX_ATTEMPTS=3 WEBHOOK_BASE_BACKOFF=0.2 \
uv run uvicorn main:app --host 127.0.0.1 --port "$API_PORT" \
  >"$WORKDIR/api.log" 2>&1 &
API_PID=$!

for _ in $(seq 1 50); do
  curl -sf "$API_BASE/health" >/dev/null 2>&1 && break
  sleep 0.1
done
curl -s "$API_BASE/health"; echo

post() { curl -s -H 'Content-Type: application/json' "$@"; }

echo "== 3. 注册订阅方 =="
GOOD=$(post -X POST "$API_BASE/subscribers" -d '{"name":"good","url":"'"$RCV_BASE"'/hook/ok","secret":"local-secret-good","event_types":["order.created"]}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
FLAKY=$(post -X POST "$API_BASE/subscribers" -d '{"name":"flaky","url":"'"$RCV_BASE"'/hook/500","secret":"local-secret-good","event_types":["order.created"]}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
BADSIG=$(post -X POST "$API_BASE/subscribers" -d '{"name":"badsig","url":"'"$RCV_BASE"'/hook/badsig","secret":"local-secret-good","event_types":["order.created"]}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
TOGGLE=$(post -X POST "$API_BASE/subscribers" -d '{"name":"toggle","url":"'"$RCV_BASE"'/hook/ok","secret":"local-secret-good","event_types":["order.created"]}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -X POST "$API_BASE/subscribers/$TOGGLE/disable" >/dev/null
echo "good=$GOOD flaky=$FLAKY badsig=$BADSIG toggle=$TOGGLE (已停用)"

echo "== 4. 提交事件 + 一次重复提交（应被去重）=="
post -X POST "$API_BASE/events" -d '{"event_id":"demo-1","event_type":"order.created","payload":{"order":"A-1001"}}' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("首次提交 deduplicated=",d["deduplicated"],"投递数=",len(d["deliveries"]))'
post -X POST "$API_BASE/events" -d '{"event_id":"demo-1","event_type":"order.created","payload":{"order":"A-1001"}}' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("重复提交 deduplicated=",d["deduplicated"],"投递数=",len(d["deliveries"]))'

echo "== 5. 等待退避重试结束（约 2s）=="
sleep 3
RESP=$(curl -s "$API_BASE/events/demo-1")
RESP="$RESP" python3 -c '
import json, os
for d in json.loads(os.environ["RESP"])["deliveries"]:
    print("  %s status=%-9s attempts=%s reason=%s" % (d["id"][:18], d["status"], d["attempts"], d["failure_reason"]))'

echo "== 6. 重新启用 toggle，提交新事件 =="
curl -s -X POST "$API_BASE/subscribers/$TOGGLE/enable" >/dev/null
post -X POST "$API_BASE/events" -d '{"event_id":"demo-2","event_type":"order.created","payload":{"order":"A-1002"}}' >/dev/null
sleep 2
curl -s "$RCV_BASE/health"; echo
echo "停用期间的 demo-1 不补投；demo-2 中 good 与 toggle 均成功。"
echo
echo "== 7. 用例小结 =="
echo "  succeeded / retries_exhausted(3 次尝试) / signature_invalid(1 次终态) 可区分；"
echo "  重复提交零新增投递；停用不投、恢复即投。详见 README.md。"
