"""
AI Router GUI - 桌面启动器

职责：
  1. 确保依赖可用（优先用软件自带 venv，缺失则回退系统 Python）
  2. 在后台线程启动 FastAPI（app.py）
  3. 用 pywebview 打开原生窗口；无 pywebview 时回退系统浏览器
  4. 退出时可靠地停止全部子服务（关键：避免 litellm 残留）

关于进程管理：
  litellm / proxy 都是本进程的子进程，且以 CREATE_NEW_PROCESS_GROUP
  启动。窗口关闭 / Ctrl+C / 调用 /api/service/stop-all 都会走到
  MANAGER.stop_all()，再由 taskkill /T 清掉整棵子进程树。
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import os
import signal
import socket
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path


# ============================================================
# 解除继承来的控制台（关键：避免残留命令行窗口）
# ============================================================

def _detach_inherited_console() -> None:
    """
    把本进程从「继承来的控制台」里解绑，避免残留一个黑色命令行窗口。

    背景（已实测确认）：
        控制台会被子进程继承。批处理里写
            start "" pythonw.exe main.py
        虽然让 cmd.exe 立即退出，但 pythonw 进程仍然附着在那个控制台上；
        只要还有进程附着，Windows 就不会关闭该控制台窗口 ——
        表现为「启动后一直挂着一个命令行窗口」。

        FreeConsole() 让本进程脱离控制台，无人附着的控制台随即关闭，
        于是只剩可视化窗口。

    仅在 pythonw.exe（窗口化启动）下执行：
    用 python.exe 从终端启动时保留控制台，方便看日志。
    """

    if os.name != "nt":
        return

    exe = (sys.executable or "").lower()
    if not exe.endswith("pythonw.exe"):
        return

    try:
        had_console = bool(ctypes.windll.kernel32.GetConsoleWindow())
        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        return

    if not had_console:
        return

    # 脱离控制台后，原先继承来的 stdout/stderr 句柄已失效，
    # 继续 print 会抛 OSError，这里换成安全的黑洞。
    try:
        devnull = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = devnull
        sys.stderr = devnull
    except Exception:
        sys.stdout = None  # type: ignore[assignment]
        sys.stderr = None  # type: ignore[assignment]

    # 标准输入句柄同样失效。子进程若继承这个无效句柄，Windows 会抛
    #   OSError [WinError 6] 句柄无效
    # 真实踩坑：find_python() 跑解释器探测、netstat / taskkill 全部失败，
    # 界面表现成「找不到带 fastapi/httpx/uvicorn 的 Python 解释器」。
    # 把 0 号句柄直接重定向到空设备，从根上消除该隐患
    # （不只是本项目自己的 subprocess，任何库拉起的子进程都受益）。
    try:
        fd = os.open(os.devnull, os.O_RDWR)
        os.dup2(fd, 0)
        if fd != 0:
            os.close(fd)
    except Exception:
        pass


# 必须在 import backend / app（会做依赖探测、拉起 uvicorn）之前执行，
# 否则命令行窗口会多停留数秒。
_detach_inherited_console()


# 让 import backend / app 生效
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backend  # noqa: E402
from backend import MANAGER  # noqa: E402


HOST = "127.0.0.1"
DEFAULT_UI_PORT = 5178
WINDOW_TITLE = "AI Router 控制台"

# 窗口化启动（pythonw.exe）时没有控制台，print 内容会被丢弃。
# 因此把未捕获异常写到这里，方便排查启动失败等问题。
CRASH_LOG = Path(__file__).resolve().parent / "gui_error.log"


def log_crash(exc_text: str) -> None:
    """把异常堆栈追加写入 gui_error.log（失败也不能再抛）。"""

    try:
        with open(CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(
                f"\n[{datetime.now().isoformat(timespec='seconds')}]\n"
                f"{exc_text}\n"
            )
    except Exception:
        pass


# ============================================================
# 端口选择
# ============================================================


def pick_port(preferred: int) -> int:
    """优先用首选端口；被占用则向上找一个空闲端口。"""

    for port in range(preferred, preferred + 40):
        if not backend.port_listening(port, HOST):
            return port
    # 极端情况：返回 0 让系统分配
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


# ============================================================
# 后台 HTTP 服务
# ============================================================


def start_server(port: int) -> threading.Thread:
    """在后台线程跑 uvicorn。"""

    import uvicorn
    from app import app

    config = uvicorn.Config(
        app,
        host=HOST,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    def run():
        try:
            server.run()
        except Exception as e:
            print(f"[gui] HTTP 服务异常退出：{e}", file=sys.stderr)

    thread = threading.Thread(target=run, name="http-server", daemon=True)
    thread.start()
    return thread


def wait_server(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if backend.port_listening(port, HOST):
            return True
        time.sleep(0.2)
    return False


# ============================================================
# 退出清理
# ============================================================

_cleanup_done = threading.Event()


def cleanup() -> None:
    """
    停止所有子服务。

    用 Event 保证只执行一次——atexit、signal、窗口关闭可能同时触发。
    """

    if _cleanup_done.is_set():
        return
    _cleanup_done.set()

    try:
        print("[gui] 正在停止子服务 ...")
        MANAGER.stop_all()
        print("[gui] 子服务已全部停止")
    except Exception as e:
        print(f"[gui] 停止子服务出错：{e}", file=sys.stderr)


atexit.register(cleanup)


def install_signal_handlers() -> None:
    """让 Ctrl+C / 终止信号也走清理流程。"""

    def handler(signum, frame):
        cleanup()
        # 恢复默认行为，避免信号被吞导致无法退出
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except Exception:
            pass


# ============================================================
# 主入口
# ============================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Router 桌面控制台")
    parser.add_argument("--port", type=int, default=DEFAULT_UI_PORT,
                        help=f"GUI 端口，默认 {DEFAULT_UI_PORT}")
    parser.add_argument("--browser", action="store_true",
                        help="强制使用系统浏览器，不开原生窗口")
    parser.add_argument("--no-window", action="store_true",
                        help="只启动后端，不开任何界面（调试用）")
    args = parser.parse_args()

    install_signal_handlers()

    ui_port = pick_port(args.port)

    print("=" * 58)
    print(" AI Router 控制台")
    print("=" * 58)

    deps = MANAGER.deps()
    print(f" 项目目录 : {deps['root']}")
    print(f" Python   : {deps['python'] or '(未找到)'}")
    print(f" litellm  : {deps['litellm'] or '(未找到)'}")
    if not deps["ok"]:
        print()
        print(" [!] 依赖不完整，界面仍会打开，但服务无法启动。")
        print("     请按界面提示安装缺失依赖。")

    start_server(ui_port)

    if not wait_server(ui_port):
        print("[gui] HTTP 服务启动超时", file=sys.stderr)
        return 1

    url = f"http://{HOST}:{ui_port}/"
    print(f" 界面地址 : {url}")
    print("=" * 58)
    print()

    if args.no_window:
        print(" 已以无界面模式启动，Ctrl+C 退出。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        cleanup()
        return 0

    # ---------- 原生窗口 ----------

    use_webview = not args.browser

    if use_webview:
        try:
            import webview  # type: ignore

            window = webview.create_window(
                WINDOW_TITLE,
                url,
                width=1280,
                height=840,
                min_size=(980, 640),
                text_select=True,
            )

            # 窗口真正关闭后再清理，避免界面卡住时服务被提前杀掉
            def on_closed():
                cleanup()

            window.events.closed += on_closed

            print(" 正在打开原生窗口 ...")
            webview.start()
            cleanup()
            return 0

        except ImportError:
            print(" [!] 未安装 pywebview，回退到系统浏览器。")
            print("     安装原生窗口：pip install pywebview")
            print()

    # ---------- 浏览器回退 ----------

    webbrowser.open(url)
    print(" 已在浏览器中打开界面。")
    print(" 关闭本窗口（或 Ctrl+C）将停止所有服务。")
    print()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    cleanup()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        # 窗口化启动时看不到控制台，把崩溃信息落盘
        log_crash(traceback.format_exc())
        raise
