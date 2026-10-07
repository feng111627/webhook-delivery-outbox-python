"""本地启动入口：API 服务 + 后台投递 worker。"""
from __future__ import annotations

import uvicorn

from app.api import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
