"""命令行入口。

* ``uv run python -m webhook.cli server``    启动投递服务（:8080）
* ``uv run python -m webhook.cli receiver``  启动模拟接收端（:9100）
* ``uv run python -m webhook.cli seed``      按 config/subscriptions.json
                                             注册本地订阅方
* ``uv run python -m webhook.cli demo``      receiver + server + seed
                                             一条龙（本地验证用）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import uvicorn

from webhook.app import build_components
from webhook.config import load_settings
from webhook.db import init_db


def _seed(config_path: str, db_path: str) -> int:
    raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    service, worker, deliverer, conn = build_components(db_path)
    init_db(conn)
    count = 0
    for item in raw.get("subscriptions", []):
        try:
            service.register_subscription(
                name=item["name"],
                url=item["url"],
                secret=item["secret"],
                event_types=item["event_types"],
            )
            count += 1
            print(f"  registered: {item['name']} -> {item['url']}")
        except Exception as exc:  # 已存在等情况跳过但提示
            print(f"  skipped {item.get('name')}: {exc}")
    conn.close()
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="webhook")
    parser.add_argument("--settings", default="config/settings.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p_server = sub.add_parser("server", help="启动投递服务")
    p_server.add_argument("--host", default="127.0.0.1")
    p_server.add_argument("--port", type=int, default=8080)

    p_recv = sub.add_parser("receiver", help="启动本地模拟接收端")
    p_recv.add_argument("--host", default="127.0.0.1")
    p_recv.add_argument("--port", type=int, default=9100)

    p_seed = sub.add_parser("seed", help="从本地配置注册订阅方")
    p_seed.add_argument(
        "--subscriptions", default="config/subscriptions.json"
    )

    p_demo = sub.add_parser("demo", help="同时启动接收端+投递服务并完成 seed")
    p_demo.add_argument("--subscriptions", default="config/subscriptions.json")

    args = parser.parse_args(argv)
    settings = load_settings(args.settings)

    if args.command == "server":
        uvicorn.run(
            "main:app",
            host=args.host,
            port=args.port,
        )
        return 0

    if args.command == "receiver":
        uvicorn.run(
            "webhook.receiver:create_receiver_app",
            factory=True,
            host=args.host,
            port=args.port,
        )
        return 0

    if args.command == "seed":
        n = _seed(args.subscriptions, settings.database_path)
        print(f"seed 完成，新注册 {n} 个订阅方")
        return 0

    if args.command == "demo":
        # 先 seed，再在本进程内同事件循环起两个服务
        n = _seed(args.subscriptions, settings.database_path)
        print(f"seed 完成，新注册 {n} 个订阅方")
        import asyncio

        async def _serve():
            config = uvicorn.Config(
                "webhook.receiver:create_receiver_app",
                factory=True,
                host="127.0.0.1",
                port=9100,
                log_level="warning",
            )
            recv_server = await config.serve()
            server = uvicorn.Server(
                uvicorn.Config(
                    "main:app", host="127.0.0.1", port=8080, log_level="warning"
                )
            )
            await server.serve()
            recv_server.should_exit = True

        asyncio.run(_serve())
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
