#!/usr/bin/env bash
# 一键本地验证：启动模拟接收端 + 投递服务，提交演示事件，打印各订阅方终态。
# 全程只用 127.0.0.1 与本地密钥，不访问任何外部服务。
set -euo pipefail

cd "$(dirname "$0")/.."
SETTINGS=/tmp/webhook-local-demo-settings.json
DB=/tmp/webhook-local-demo.db
cat > "$SETTINGS" <<JSON
{"database_path":"$DB","poll_interval":0.1,"http_timeout":2.0,
 "max_attempts":4,"base_delay":0.5,"max_delay":2.0,"timestamp_skew_seconds":300}
JSON
rm -f "$DB" "$DB"-wal "$DB"-shm

cleanup() {
  [ -n "${RECV_PID:-}" ] && kill "$RECV_PID" 2>/dev/null || true
  [ -n "${SRV_PID:-}" ] && kill "$SRV_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> 启动模拟接收端 :9100"
uv run python -m uvicorn webhook.receiver:create_receiver_app --factory \
  --host 127.0.0.1 --port 9100 --log-level warning &
RECV_PID=$!

echo "==> seed 本地订阅方"
uv run python -m webhook.cli --settings "$SETTINGS" seed >/dev/null

echo "==> 启动投递服务 :8080"
WEBHOOK_SETTINGS="$SETTINGS" uv run python - <<'PY' &
import os, uvicorn
from webhook.app import create_app
from webhook.config import load_settings
s = load_settings(os.environ["WEBHOOK_SETTINGS"])
app = create_app(s.database_path, True, s)
uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")
PY
SRV_PID=$!

sleep 2.5
post() { curl -s -o /dev/null -w "%{http_code}" -X POST "$1" -H 'Content-Type: application/json' -d "$2"; }

echo "==> 首次提交 demo-order-1（期望 202）: $(post http://127.0.0.1:8080/api/v1/events '{"event_id":"demo-order-1","event_type":"order.created","payload":{"order_id":"A-1"}}')"
echo "==> 重复提交 demo-order-1（期望 200）: $(post http://127.0.0.1:8080/api/v1/events '{"event_id":"demo-order-1","event_type":"order.created","payload":{"order_id":"A-1"}}')"
echo "==> 提交 demo-pay-1（期望 202）:       $(post http://127.0.0.1:8080/api/v1/events '{"event_id":"demo-pay-1","event_type":"order.paid","payload":{"order_id":"A-1"}}')"

echo "==> 等待退避重试全部结束（约 15 秒）..."
sleep 15

uv run python - <<'PY'
import json, urllib.request
for eid in ("demo-order-1", "demo-pay-1"):
    data = json.load(urllib.request.urlopen(f"http://127.0.0.1:8080/api/v1/events/{eid}"))
    print(f"\n[{eid}]")
    for d in data["deliveries"]:
        print(f"  {d['subscription_name']:18s} {d['status']:38s} "
              f"attempts={d['attempts']} reason={d['last_failure_reason']}")
PY
echo
echo "==> 单测结果："
uv run pytest -q
