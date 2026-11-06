# -*- coding: utf-8 -*-
"""
医智呼一键启动辅助程序（由上一级的「开始.bat」调用，请勿单独运行）。

使用方法：
    双击 D:\\Ai\\开始.bat 即可，本文件会被自动调用。

本程序负责：
    1. 自动 CD 到项目根目录（即本文件所在目录 D:\\Ai\\app）。
    2. 从 8080 起检测端口；被占用则自动顺延，最多尝试 60 个端口，
       并把实际端口通过 PORT 环境变量交给后端。
    3. 检查依赖；缺失时自动 pip 安装 requirements.txt（清华镜像）。
    4. 启动「后端 + 前端」：本项目的前端页面位于 app/static，由 FastAPI
       统一托管，同一个 uvicorn 服务同时提供 /api/* 接口与静态页面，
       所以一个进程即可让前后端都就绪，拆成两个进程反而会跨域故障。
    5. 就绪轮询分别检查 /api/health（后端）与 /（前端首页），
       两者都返回 200 之后，才调用系统默认浏览器打开系统。
    6. 持续运行服务；关闭黑色命令行窗口即可停止。

可选环境变量：
    YZH_BASE_PORT   起始端口，默认 8080
    YZH_NO_BROWSER  设为 1 时不自动打开浏览器（仅调试用）
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BASE_PORT = int(os.getenv("YZH_BASE_PORT", "8080"))
MAX_PORT_TRIES = 60
READY_TIMEOUT = 90


def pick_port(start: int) -> int:
    """从 start 起找第一个空闲端口。Windows 下不设 SO_REUSEADDR，否则会误判占用。"""
    for port in range(start, start + MAX_PORT_TRIES):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"{start} ~ {start + MAX_PORT_TRIES - 1} 端口全部被占用")


def ensure_dependencies() -> None:
    needed = ("fastapi", "uvicorn", "sqlalchemy", "httpx", "multipart", "openpyxl")
    missing = [name for name in needed if not _can_import(name)]
    if not missing:
        return
    print(f"[安装] 缺少依赖：{', '.join(missing)}，正在自动安装 requirements.txt ...")
    subprocess.check_call([
        sys.executable, "-m", "pip", "install",
        "-r", os.path.join(ROOT, "requirements.txt"),
        "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
        "--disable-pip-version-check",
    ])


def _can_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_and_open(base: str) -> None:
    """分别确认后端 API 与前端页面就绪，然后打开默认浏览器。"""
    deadline = time.time() + READY_TIMEOUT
    api_ready = False
    page_ready = False
    static_ready = False
    while time.time() < deadline:
        if not api_ready:
            api_ready = http_ok(base + "/api/health")
        if not page_ready:
            page_ready = http_ok(base + "/")
        if api_ready and page_ready and not static_ready:
            static_ready = http_ok(base + "/static/app.js")
        if api_ready and page_ready and static_ready:
            break
        time.sleep(0.5)

    if not (api_ready and page_ready and static_ready):
        print(f"[失败] 启动超时：后端={api_ready} 前端首页={page_ready} "
              f"静态资源={static_ready}，请查看上方日志。")
        return

    print(f"[就绪] 后端 API + 前端页面 + 静态资源 全部就绪：{base}/")
    print("[就绪] 正在打开默认浏览器 ...")
    if os.getenv("YZH_NO_BROWSER") != "1":
        webbrowser.open(base + "/")


def main() -> None:
    ensure_dependencies()
    port = pick_port(BASE_PORT)
    if port == BASE_PORT:
        print(f"[端口] {BASE_PORT} 可用，使用端口 {port}")
    else:
        print(f"[端口] {BASE_PORT} 已被占用，已自动避开，改用端口 {port}")

    base = f"http://127.0.0.1:{port}"
    os.environ["PORT"] = str(port)

    threading.Thread(target=wait_and_open, args=(base,), daemon=True).start()

    import uvicorn
    print(f"[启动] 服务地址：{base}/")
    print("[提示] 保持本窗口开启；关闭窗口或按 Ctrl+C 即停止服务。")
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[停止] 已手动停止服务。")
    except Exception as exc:
        print(f"[错误] {type(exc).__name__}: {exc}")
        sys.exit(1)
