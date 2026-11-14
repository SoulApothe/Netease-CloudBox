"""云音盒子 · 桌面版启动入口

双击「启动桌面版.bat」即可运行：自动准备依赖 -> 随机空闲端口启动本地服务
-> 打开原生桌面窗口（Edge WebView2 / Python WebView）-> 关窗即退出。

优先级：
  1. pywebview + WebView2 原生窗口（无地址栏、可调系统文件夹对话框）
  2. Edge / Chrome 的 --app 独立窗口（零额外依赖，类原生体验）
  3. 系统默认浏览器（最兜底）

也可用：
  python desktop.py --server          仅启动本地 Web 服务（兼容旧用法）
  python desktop.py --browser         强制用默认浏览器打开
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from config import APP_NAME, BASE_DIR, VERSION, output_dir
from server import app


def log_line(message: str) -> None:
    """把桌面启动过程写进 music_api.log：打包成无控制台的 exe 后，这是唯一的排查线索。"""
    try:
        with open(BASE_DIR / "music_api.log", "a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [桌面] {message}\n")
    except OSError:
        pass


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(port: int):
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, name="cloudbox-http", daemon=True)
    thread.start()
    return server


def wait_ready(url: str, timeout: float = 8.0) -> bool:
    """等待本地服务就绪；显式绕过系统代理，避免 127.0.0.1 被代理劫持。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with opener.open(url + "health", timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


class DesktopApi:
    """暴露给前端 window.pywebview.api 的原生能力。"""

    def __init__(self, window=None):
        self._window = window

    def choose_folder(self):
        try:
            import webview
            result = self._window.create_file_dialog(
                webview.FOLDER_DIALOG, directory=str(BASE_DIR)
            )
            if result and isinstance(result, (list, tuple)):
                return str(result[0])
            return str(result) if result else ""
        except Exception as exc:  # noqa: BLE001
            return f"__ERROR__{exc}"

    def open_folder(self, path: str = ""):
        target = Path(path) if path else output_dir()
        if not target.is_dir():
            target = output_dir()
        try:
            if sys.platform == "win32":
                os.startfile(str(target))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
            return True
        except Exception:
            return False

    def app_info(self):
        return {"name": APP_NAME, "version": VERSION, "output_dir": str(output_dir())}


def try_webview(url: str, server) -> bool:
    """使用 pywebview + WebView2 打开原生桌面窗口（阻塞直到关窗）。"""
    try:
        import webview  # noqa: F401
        from webview.platforms import edgechromium  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"[桌面] WebView2 不可用，改用浏览器窗口：{exc}")
        log_line(f"WebView2 不可用，改用浏览器窗口：{exc}")
        return False
    try:
        api = DesktopApi()
        window = webview.create_window(
            title=APP_NAME,
            url=url,
            width=1320,
            height=880,
            min_size=(1080, 700),
            background_color="#F5F6FA",
            js_api=api,
            text_select=False,
            confirm_close=False,
        )
        api._window = window
        log_line("原生窗口已创建，开始加载页面")
        webview.start(gui="edgechromium", debug=False, private_mode=False)
        log_line("原生窗口已关闭，程序退出")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[桌面] 原生窗口启动失败，改用浏览器窗口：{exc}")
        log_line(f"原生窗口启动失败，改用浏览器窗口：{exc}")
        return False


def find_browser() -> str:
    candidates = []
    for env_key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(env_key)
        if not root:
            continue
        candidates += [
            str(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
            str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"),
        ]
    candidates += [shutil.which("msedge"), shutil.which("chrome")]
    for path in candidates:
        if path and Path(path).is_file():
            return path
    return ""


def try_app_window(url: str, server) -> bool:
    """Edge/Chrome --app 独立窗口：无标签栏 / 地址栏，观感接近原生应用。"""
    browser = find_browser()
    if not browser:
        return False
    profile = tempfile.mkdtemp(prefix="cloudbox-profile-")
    try:
        proc = subprocess.Popen([
            browser,
            f"--app={url}",
            "--window-size=1320,880",
            "--window-position=120,60",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=msEdgeSidebarV2,msUndersideButton",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        print(f"[桌面] 浏览器窗口启动失败：{exc}")
        return False
    print("[桌面] 已打开独立应用窗口，关闭窗口即退出程序。")
    log_line(f"WebView2 不可用，已用 {browser} 打开独立应用窗口")
    try:
        while proc.poll() is None:
            time.sleep(0.4)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    log_line("应用窗口已关闭，程序退出")
    return True


def banner(url: str, port: int, mode: str) -> None:
    line = "=" * 62
    print()
    print(line)
    print(f"  {APP_NAME}  v{VERSION}   ·   桌面版")
    print(line)
    print(f"  本地服务 : {url}")
    print(f"  监听地址 : 127.0.0.1:{port} （仅本机可访问）")
    print(f"  歌曲输出 : {output_dir()}")
    print(f"  窗口模式 : {mode}")
    print(line)
    print("  提示：会员音质需要有效的会员 Cookie，可在「设置」里扫码登录。")
    print(line)
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} 桌面版")
    parser.add_argument("--server", action="store_true", help="仅启动 Web 服务，不打开窗口")
    parser.add_argument("--browser", action="store_true", help="强制使用默认浏览器")
    parser.add_argument("--port", type=int, default=0, help="指定端口（默认自动选择空闲端口）")
    args = parser.parse_args()

    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}/"
    server = start_server(port)
    if not wait_ready(url):
        print("[桌面] 本地服务启动超时，请查看 music_api.log")
        log_line("本地服务启动超时")
        server.shutdown()
        return 1
    log_line(f"本地服务已就绪 v{VERSION} {url} 输出目录 {output_dir()}")

    if args.server:
        banner(url, port, "仅服务模式（Ctrl+C 退出）")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        server.shutdown()
        return 0

    banner(url, port, "原生桌面窗口（WebView2）")
    if args.browser:
        import webbrowser
        webbrowser.open(url)
        print("[桌面] 已在默认浏览器打开，按 Ctrl+C 或关闭本窗口退出。")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        server.shutdown()
        return 0

    if try_webview(url, server):
        server.shutdown()
        return 0
    if try_app_window(url, server):
        return 0

    import webbrowser
    webbrowser.open(url)
    print("[桌面] 已在默认浏览器打开，按 Ctrl+C 或关闭本窗口退出。")
    log_line("WebView2 与 Edge/Chrome 均不可用，已改用系统默认浏览器")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    server.shutdown()
    return 0


def fatal_dialog(message: str) -> None:
    """打包成无控制台的 exe 后，出错时用系统弹窗告知用户（而不是静默退出）。"""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, f"{APP_NAME} 启动失败", 0x10)
            return
        except Exception:  # noqa: BLE001
            pass
    print(message)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已退出。")
        sys.exit(0)
    except Exception:  # noqa: BLE001
        import traceback

        detail = traceback.format_exc()
        try:
            with open(BASE_DIR / "music_api.log", "a", encoding="utf-8") as handle:
                handle.write("\n[启动异常]\n" + detail + "\n")
        except OSError:
            pass
        fatal_dialog(
            f"{APP_NAME} 启动失败，详细信息已写入同目录的 music_api.log。\n\n"
            + detail.strip().splitlines()[-1][:300]
        )
        sys.exit(1)
