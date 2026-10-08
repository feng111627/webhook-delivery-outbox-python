"""开发入口：uv run python main.py 启动投递服务。"""
from __future__ import annotations

import uvicorn

from webhook.app import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8080, reload=False)
