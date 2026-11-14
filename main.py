"""云音盒子 · 兼容入口

旧版本用 `python main.py` 启动 Flask 网页服务；现在默认直接启动桌面版窗口。
    python main.py                  启动桌面版（等价 python desktop.py）
    python main.py --server         仅启动本地服务（默认沿用 5000 端口，兼容旧文档）
    python main.py --browser        用默认浏览器打开

`from main import app` 仍然可用，返回 Flask 应用对象。
"""
from __future__ import annotations

import sys

from server import app  # noqa: F401  (兼容旧代码的导入)
from desktop import main as desktop_main


def start_api_server():
    """保留旧函数名，避免外部脚本调用时报错。"""
    if "--server" not in sys.argv:
        sys.argv.append("--server")
    if "--port" not in sys.argv:
        sys.argv.extend(["--port", "5000"])
    return desktop_main()


if __name__ == "__main__":
    if "--server" in sys.argv and "--port" not in sys.argv:
        sys.argv.extend(["--port", "5000"])
    sys.exit(desktop_main())
