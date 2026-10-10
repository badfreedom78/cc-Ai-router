"""
AI Router GUI - 后端服务层

负责：
  - 进程编排：以子进程方式启动 / 停止 LiteLLM 与 Router Proxy
  - 状态聚合：端口监听、健康检查、进程存活
  - 日志缓冲：捕获两个服务的 stdout/stderr，环形缓冲 + 订阅推送
  - 配置读写：安全地读写 config.yaml 的 model_list

设计原则：
  - 所有依赖探测都基于项目根目录，不写死绝对路径
  - config.yaml 始终保持纯 ASCII 可解析（中文注释会导致
    LiteLLM 在 GBK 默认编码下 yaml.safe_load 崩溃）
  - 停止服务时先 graceful terminate，超时再 kill

本模块不依赖 FastAPI，可独立被 main.py（pywebview 桌面壳）
或 app.py（HTTP 服务）复用。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore


# ============================================================
# 路径与常量
# ============================================================

# gui/ 的上一级就是项目根目录
GUI_DIR = Path(__file__).resolve().parent
ROOT = GUI_DIR.parent

CONFIG_YML = ROOT / "config.yaml"
PROXY_PY = ROOT / "proxy.py"
ROUTER_PY = ROOT / "router.py"

# 强制路由覆盖文件（由「强制选择」功能写入，proxy.py 每次请求读取）
ROUTE_OVERRIDE = ROOT / "router_override.json"

# 问答日志（由 proxy.py 追加写入，GUI 用量面板读取）
QA_LOG_PATH = ROOT / "logs" / "qa_log.jsonl"

DEFAULT_LITELLM_PORT = 4000
DEFAULT_PROXY_PORT = 4001
DEFAULT_MASTER_KEY = "sk-local-router-master-key"

# 界面语言设置（仅影响 GUI 文案，不影响路由逻辑）
LANG_FILE = ROOT / "gui_settings.json"
DEFAULT_LANGUAGE = "zh"

# 日志环形缓冲上限（行）
LOG_BUFFER_SIZE = 4000

# 停止服务时的宽限期（秒）
TERMINATE_GRACE = 8.0

# Windows：禁止子进程附着 / 弹出控制台窗口。
#
# GUI 用 pythonw.exe 启动时会继承 cmd.exe 的控制台，而 litellm.exe /
# python.exe / taskkill.exe 都是控制台程序。若不显式隔离，它们会挂在
# 同一个控制台上，导致那个命令行窗口在 cmd 退出后依然不关闭。
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _run(cmd, **kwargs):
    """
    统一封装 subprocess.run，强制隔离三个标准流。

    为什么必须显式指定 stdin=DEVNULL：
    GUI 用 pythonw.exe 启动，并在 import backend 之前调用 FreeConsole()
    脱离控制台。此后进程不再持有有效的标准句柄，若子进程未显式指定
    stdin，Windows 会直接抛 OSError [WinError 6] 句柄无效 —— 这会让
    解释器探测、netstat、taskkill、版本探测全部失败。

    （真实症状：界面点「启动 Router Proxy」报
      「找不到带 fastapi/httpx/uvicorn 的 Python 解释器」）
    """

    kwargs.setdefault("stdin", subprocess.DEVNULL)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("creationflags", NO_WINDOW)
    return subprocess.run(cmd, **kwargs)


# ============================================================
# 日志缓冲与订阅
# ============================================================


def _decode_line(raw: bytes) -> str:
    """
    容错解码一行子进程输出。

    Windows 上各工具输出编码不统一：
      - Python / uvicorn 通常为 UTF-8
      - litellm 的 ASCII 艺术横幅常为 GBK（如框线字符 0xbb）

    策略：先试 UTF-8，失败再试 GBK，最后用 replace 兜底。
    由于纯 ASCII 在两种编码下完全一致，因此绝不会有侥幸正确的问题。
    """

    if isinstance(raw, str):
        return raw

    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue

    return raw.decode("utf-8", errors="replace")


# ANSI 颜色 / 光标控制序列。litellm 在检测到 TTY 时会输出彩色日志，
# 经子进程管道捕获后会变成难读的 \x1b[32m 之类，这里统一剔除。
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    """去掉 ANSI 转义序列。"""

    if "\x1b" not in text:
        return text
    return _ANSI_RE.sub("", text)


class LogStream:
    """
    单个服务（litellm / proxy）的日志缓冲。

    维护一个环形缓冲，并向所有订阅者（SSE 连接）广播新行。
    线程安全：_lock 保护缓冲，订阅者各自持有 queue。
    """

    def __init__(self, name: str, buffer_size: int = LOG_BUFFER_SIZE):
        self.name = name
        self._lock = threading.Lock()
        self._buffer: deque[dict] = deque(maxlen=buffer_size)
        self._subscribers: list[Callable[[dict], None]] = []
        self._seq = 0

    def append(self, text: str, level: str = "info") -> None:
        """追加一行日志（自动去掉行尾换行与 ANSI 转义序列）。"""

        line = _strip_ansi(text.rstrip("\r\n"))
        if not line:
            return

        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "ts": datetime.now().strftime("%H:%M:%S"),
                "level": level,
                "text": line,
            }
            self._buffer.append(entry)
            subs = list(self._subscribers)

        for cb in subs:
            try:
                cb(entry)
            except Exception:
                # 单个订阅者出错不能影响进程读取
                pass

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    def snapshot(self, since: int = 0) -> list[dict]:
        """返回 seq > since 的所有日志行。"""

        with self._lock:
            return [e for e in self._buffer if e["seq"] > since]

    def subscribe(self, cb: Callable[[dict], None]) -> Callable[[], None]:
        """注册订阅者，返回取消订阅的函数。"""

        with self._lock:
            self._subscribers.append(cb)

        def unsubscribe() -> None:
            with self._lock:
                if cb in self._subscribers:
                    self._subscribers.remove(cb)

        return unsubscribe


# ============================================================
# 依赖探测
# ============================================================


# 最近一次 find_python() 的探测详情。
# 用途：探测失败时把「试过哪些解释器、各自为什么不行」原样告诉用户，
# 而不是只说一句「找不到解释器」。
LAST_PYTHON_DIAG: list[str] = []


def find_python() -> str | None:
    """
    找到第一个能 import fastapi/httpx/uvicorn 的 Python 解释器。

    逐个探测候选解释器，返回第一个可用者，不依赖 PowerShell。
    """

    candidates: list[str] = []

    # 当前解释器优先（GUI 本身就是用它跑起来的）。
    #
    # 注意：窗口化启动时 sys.executable 是 pythonw.exe，
    # 它没有控制台。虽然管道重定向下仍能读写 stdout，
    # 但为避免任何意外，这里换成同目录的 python.exe，
    # 让 litellm / proxy 子进程始终有正常的控制台语义。
    exe = sys.executable or ""
    if exe.lower().endswith("pythonw.exe"):
        sibling = exe[: -len("pythonw.exe")] + "python.exe"
        if os.path.exists(sibling):
            exe = sibling
    if exe:
        candidates.append(exe)

    # 项目自带的 venv：依赖就装在这里，优先级仅次于当前解释器。
    # 即使 GUI 用别的 Python 启动、或系统 Python 缺依赖，
    # 也能回退到这里，避免 find_python() 返回 None
    # 导致「启动 Proxy」直接报错。
    venv_root = ROOT / "gui" / ".venv"
    for rel in ("Scripts/python.exe", "bin/python"):
        venv_py = venv_root / rel
        if venv_py.exists():
            candidates.append(str(venv_py))

    # PATH 上的 python
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    # 常见系统安装位置
    candidates += [
        r"C:\Python312\python.exe",
        r"C:\Python313\python.exe",
        r"C:\Python311\python.exe",
    ]

    seen: set[str] = set()

    LAST_PYTHON_DIAG.clear()

    for exe in candidates:
        if not exe or exe in seen:
            continue
        seen.add(exe)
        if not os.path.exists(exe):
            LAST_PYTHON_DIAG.append(f"{exe} —— 文件不存在")
            continue
        try:
            proc = _run(
                [exe, "-c", "import fastapi, httpx, uvicorn, yaml"],
                timeout=20,
            )
            if proc.returncode == 0:
                LAST_PYTHON_DIAG.append(f"{exe} —— 可用")
                return exe

            err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            tail = err.splitlines()[-1] if err else f"退出码 {proc.returncode}"
            LAST_PYTHON_DIAG.append(f"{exe} —— {tail}")
        except Exception as e:
            LAST_PYTHON_DIAG.append(f"{exe} —— 探测失败：{e}")

    return None


def find_litellm() -> str | None:
    """定位 litellm 可执行文件。"""

    for name in ("litellm", "litellm.exe"):
        found = shutil.which(name)
        if found:
            return found

    home = Path.home()
    shims = [
        home / ".local" / "bin" / "litellm.exe",
        home / ".local" / "bin" / "litellm",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "uv"
        / "tools"
        / "litellm"
        / "Scripts"
        / "litellm.exe",
    ]
    for s in shims:
        if s.exists():
            return str(s)

    return None


# ============================================================
# 端口工具
# ============================================================


def port_listening(port: int, host: str = "127.0.0.1") -> bool:
    """端口是否处于 LISTEN 状态（真实连接探测，不依赖 netstat）。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        try:
            return s.connect_ex((host, port)) == 0
        except Exception:
            return False


def kill_port_owner(port: int) -> list[int]:
    """
    结束占用指定端口的进程。

    返回被结束的 PID 列表。使用 netstat 解析，
    因为纯 Python 拿不到 Windows 上的 owner PID。
    """

    killed: list[int] = []

    out = ""
    try:
        proc = _run(
            ["netstat", "-ano", "-p", "TCP"],
            timeout=15,
        )
        # netstat 在中文 Windows 上输出为 GBK/ANSI，
        # 这里拿到 bytes 后自行解码，避免 text=True 触发 UnicodeDecodeError。
        # 端口 / PID / 状态字段都是 ASCII，errors="replace" 足够安全。
        raw = proc.stdout or b""
        out = raw.decode("utf-8", errors="replace")
        if "\ufffd" in out:
            out = raw.decode("gbk", errors="replace")
    except Exception:
        return killed

    if not out:
        return killed

    pattern = re.compile(
        rf"^\s*TCP\s+\S+:{port}\s+\S+\s+LISTENING\s+(\d+)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )

    for match in pattern.finditer(out):
        pid = int(match.group(1))
        if pid <= 0:
            continue
        try:
            _run(
                ["taskkill", "/PID", str(pid), "/F", "/T"],
                timeout=15,
            )
            killed.append(pid)
        except Exception:
            pass

    return killed


# ============================================================
# 配置读写
# ============================================================


# config.yaml 会被 LiteLLM 以系统默认编码（中文 Windows 为 GBK）
# 读取，任何非 ASCII 字符都会导致 UnicodeDecodeError。
# 因此写盘前强制转义所有非 ASCII 字符，保证文件恒为纯 ASCII。
def _to_ascii(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")


def load_config() -> dict:
    """读取 config.yaml。"""

    if yaml is None:
        raise RuntimeError("缺少 PyYAML 依赖，请先安装 pyyaml")

    if not CONFIG_YML.exists():
        raise FileNotFoundError(f"找不到 {CONFIG_YML}")

    with open(CONFIG_YML, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return data


def save_config(data: dict) -> None:
    """
    原子写入 config.yaml。

    先写临时文件再替换，避免中途失败导致配置损坏。
    输出强制 ASCII，防止 GBK 解码崩溃。
    """

    if yaml is None:
        raise RuntimeError("缺少 PyYAML 依赖，请先安装 pyyaml")

    dumped = yaml.safe_dump(
        data,
        allow_unicode=False,
        sort_keys=False,
        default_flow_style=False,
        width=4096,
    )
    dumped = _to_ascii(dumped)

    tmp = CONFIG_YML.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="ascii", newline="\n") as f:
        f.write(dumped)

    os.replace(tmp, CONFIG_YML)


def get_models() -> list[dict]:
    """
    返回前端友好的模型列表。

    展开 LiteLLM 的 litellm_params / model_info 嵌套结构，
    只暴露界面需要编辑的字段。tier 按模型名前缀推导，
    供前端分组展示（router / easy / balanced / strong / 未分组）。
    """

    data = load_config()
    models: list[dict] = []

    tier_order = {"router": 0, "easy": 1, "balanced": 2, "strong": 3}

    for idx, entry in enumerate(data.get("model_list", []) or []):
        params = entry.get("litellm_params", {}) or {}
        info = entry.get("model_info", {}) or {}

        name = entry.get("model_name", "")

        tier = None
        for t in tier_order:
            if name == t or name.startswith(t + "-"):
                tier = t
                break

        models.append(
            {
                "index": idx,
                "model_name": name,
                "tier": tier,
                "model": params.get("model", ""),
                "api_base": params.get("api_base", ""),
                "api_key": params.get("api_key", ""),
                "temperature": params.get("temperature"),
                "max_tokens": params.get("max_tokens"),
                "supports_vision": bool(info.get("supports_vision", False)),
                "supports_audio_input": bool(
                    info.get("supports_audio_input", False)
                ),
            }
        )

    return models


def reorder_models(ordered_names: list[str]) -> list[dict]:
    """
    按 ordered_names 的顺序重排 model_list。

    - 顺序即调用优先级（同档位内先出现者优先被选中）；
    - 未出现在 ordered_names 里的模型保持相对顺序，排在最后；
    - 只移动列表元素，不改动任何模型的内容；
    - 写回走 save_config（ASCII 安全 + 原子替换）。
    """

    if not isinstance(ordered_names, list):
        raise ValueError("ordered_names 必须是列表")

    data = load_config()
    model_list = data.get("model_list", []) or []

    by_name: dict[str, dict] = {}
    for entry in model_list:
        name = entry.get("model_name", "")
        # 重名（不推荐）时保留最后一个，与 LiteLLM 的覆盖语义一致
        by_name[name] = entry

    unknown = [n for n in ordered_names if n not in by_name]
    if unknown:
        raise ValueError(f"未知的模型名：{', '.join(unknown)}")

    ordered: list[dict] = [by_name[n] for n in ordered_names]

    for entry in model_list:
        if entry not in ordered:
            ordered.append(entry)

    data["model_list"] = ordered
    save_config(data)

    return get_models()


def upsert_model(payload: dict) -> dict:
    """
    新增或更新一个模型。

    payload 里的 index 为 None 时新增，否则按 index 覆盖。
    返回写入后的模型对象。
    """

    data = load_config()
    model_list = data.setdefault("model_list", [])

    name = (payload.get("model_name") or "").strip()
    if not name:
        raise ValueError("model_name 不能为空")

    model = (payload.get("model") or "").strip()
    if not model:
        raise ValueError("model 不能为空")

    params: dict[str, Any] = {"model": model}

    api_base = (payload.get("api_base") or "").strip()
    if api_base:
        params["api_base"] = api_base

    api_key = (payload.get("api_key") or "").strip()
    if api_key:
        params["api_key"] = api_key

    temperature = payload.get("temperature")
    if temperature is not None and temperature != "":
        params["temperature"] = float(temperature)

    max_tokens = payload.get("max_tokens")
    if max_tokens is not None and max_tokens != "":
        params["max_tokens"] = int(max_tokens)

    entry = {
        "model_name": name,
        "litellm_params": params,
        "model_info": {
            "supports_vision": bool(payload.get("supports_vision", False)),
            "supports_audio_input": bool(
                payload.get("supports_audio_input", False)
            ),
        },
    }

    index = payload.get("index")

    if index is None or index == "" or int(index) < 0:
        model_list.append(entry)
    else:
        index = int(index)
        if index >= len(model_list):
            raise IndexError(f"index {index} 越界")
        model_list[index] = entry

    save_config(data)

    # 回读以返回规范化后的结果
    for m in get_models():
        if m["model_name"] == name:
            return m
    return {}


def delete_model(index: int) -> None:
    """按 index 删除一个模型。"""

    data = load_config()
    model_list = data.get("model_list", [])

    if index < 0 or index >= len(model_list):
        raise IndexError(f"index {index} 越界")

    model_list.pop(index)
    data["model_list"] = model_list
    save_config(data)


def claude_config_dir() -> Path:
    """
    Claude Code 的用户级配置目录。

    Windows 上为 %USERPROFILE%\\.claude，
    其他平台为 ~/.claude。
    """

    return Path.home() / ".claude"


def get_general_settings() -> dict:
    """返回 general_settings / litellm_settings 的只读摘要。"""

    data = load_config()
    return {
        "master_key": (data.get("general_settings") or {}).get(
            "master_key", DEFAULT_MASTER_KEY
        ),
        "litellm_settings": data.get("litellm_settings", {}) or {},
    }


def set_master_key(new_key: str) -> None:
    """修改 master_key（gui 与 proxy 需保持一致）。"""

    new_key = (new_key or "").strip()
    if not new_key:
        raise ValueError("master_key 不能为空")

    data = load_config()
    general = data.setdefault("general_settings", {})
    general["master_key"] = new_key
    save_config(data)


# ============================================================
# 界面语言（仅影响 GUI 文案，不参与路由）
# ============================================================


def get_language() -> str:
    """读取界面语言，默认中文。取值只有 'zh' / 'en'。"""

    try:
        if LANG_FILE.exists():
            with open(LANG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            lang = str(data.get("language", "")).strip().lower()
            if lang in ("zh", "en"):
                return lang
    except Exception:
        # 配置损坏时回落默认值，绝不影响界面启动
        pass
    return DEFAULT_LANGUAGE


def set_language(lang: str) -> str:
    """写入界面语言（原子写）。非法值一律回落默认语言。"""

    value = str(lang or "").strip().lower()
    if value not in ("zh", "en"):
        value = DEFAULT_LANGUAGE

    tmp = LANG_FILE.with_name(LANG_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"language": value}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LANG_FILE)

    return value


# ============================================================
# 强制路由（force select）
# ============================================================


def get_route_override() -> dict:
    """
    读取当前的强制路由配置。

    返回 {"force_model": str | None, "updated_at": str | None}。
    force_model 为 None 表示「智能路由」。
    """

    if not ROUTE_OVERRIDE.exists():
        return {"force_model": None, "updated_at": None}

    try:
        with open(ROUTE_OVERRIDE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        # 配置损坏时按智能路由处理，不影响主流程
        return {"force_model": None, "updated_at": None}

    model = data.get("force_model")
    if not isinstance(model, str) or not model.strip():
        model = None

    return {
        "force_model": model,
        "updated_at": data.get("updated_at"),
    }


def set_route_override(model_name: str | None) -> dict:
    """
    设置或清除强制路由。

    model_name 为空 / None -> 恢复智能路由。
    写入是原子的（先写临时文件再替换），避免读到半截文件。
    """

    name = (model_name or "").strip()

    payload = {
        "force_model": name or None,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }

    tmp = ROUTE_OVERRIDE.with_name(ROUTE_OVERRIDE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    os.replace(tmp, ROUTE_OVERRIDE)

    return payload


# ============================================================
# 用量统计（读取 proxy.py 写的问答日志）
# ============================================================


def _to_int(value) -> int | None:
    """安全转 int，失败返回 None。"""

    if value is None or isinstance(value, bool):
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# 缓存：只有日志文件的 mtime / 大小变化时才重新解析。
# 前端会周期刷新，避免每次轮询都重读整个文件。
_usage_lock = threading.Lock()
_usage_cache_key: tuple | None = None
_usage_cache_value: dict | None = None


def get_usage_stats(recent_limit: int = 10) -> dict:
    """
    汇总问答日志，供「模型配置」页的用量面板使用。

    返回：
      - recent : 最近 recent_limit 条请求（倒序，最新的在前）
      - today  : 今日按模型聚合的用量（按 token 降序）
      - totals : 今日合计
      - 以及日志路径 / 大小，便于界面直接展示
    """

    global _usage_cache_key, _usage_cache_value

    log_path = QA_LOG_PATH

    if not log_path.exists():
        return {
            "recent": [],
            "today": [],
            "totals": {
                "requests": 0,
                "in_tok": 0,
                "out_tok": 0,
                "tokens": 0,
            },
            "log_path": str(log_path),
            "log_exists": False,
            "log_size": 0,
        }

    try:
        stat = log_path.stat()
    except Exception:
        stat = None

    cache_key = (
        (str(log_path), stat.st_mtime_ns, stat.st_size)
        if stat is not None
        else None
    )

    if cache_key is not None:
        with _usage_lock:
            if (
                cache_key == _usage_cache_key
                and _usage_cache_value is not None
            ):
                return _usage_cache_value

    today_prefix = datetime.now().strftime("%Y-%m-%d")

    recent: list[dict] = []
    buckets: dict[str, dict] = {}
    totals = {"requests": 0, "in_tok": 0, "out_tok": 0, "tokens": 0}

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue

                try:
                    rec = json.loads(raw)
                except Exception:
                    # 单行损坏（如写盘中被中断）不应影响整体统计
                    continue

                if not isinstance(rec, dict):
                    continue

                item = {
                    "ts": rec.get("ts") or "",
                    "model": rec.get("model") or "-",
                    "tier": rec.get("tier"),
                    "forced": bool(rec.get("forced", False)),
                    "vision": bool(rec.get("vision", False)),
                    "in_tok": _to_int(rec.get("in_tok")),
                    "out_tok": _to_int(rec.get("out_tok")),
                    "status": _to_int(rec.get("status")),
                    "ms": _to_int(rec.get("ms")),
                    "q": rec.get("q") or "",
                }

                recent.append(item)
                if len(recent) > recent_limit:
                    recent.pop(0)

                if not item["ts"].startswith(today_prefix):
                    continue

                bucket = buckets.setdefault(
                    item["model"],
                    {
                        "model": item["model"],
                        "requests": 0,
                        "in_tok": 0,
                        "out_tok": 0,
                    },
                )
                bucket["requests"] += 1
                bucket["in_tok"] += item["in_tok"] or 0
                bucket["out_tok"] += item["out_tok"] or 0

                totals["requests"] += 1
                totals["in_tok"] += item["in_tok"] or 0
                totals["out_tok"] += item["out_tok"] or 0

    except Exception as e:
        raise RuntimeError(f"读取问答日志失败：{e}")

    today = list(buckets.values())
    for b in today:
        b["tokens"] = b["in_tok"] + b["out_tok"]

    today.sort(key=lambda x: (x["tokens"], x["requests"]), reverse=True)

    totals["tokens"] = totals["in_tok"] + totals["out_tok"]

    result = {
        "recent": list(reversed(recent)),
        "today": today,
        "totals": totals,
        "log_path": str(log_path),
        "log_exists": True,
        "log_size": stat.st_size if stat is not None else 0,
    }

    if cache_key is not None:
        with _usage_lock:
            _usage_cache_key = cache_key
            _usage_cache_value = result

    return result


# ============================================================
# 外部依赖：版本探测与一键升级
# ============================================================


# 需要在「设置」页展示 / 升级的外部依赖
# key   -> 探测脚本里的字段名
# label -> 界面显示名
# dist  -> importlib.metadata 里的分发包名（PyPI 名称）
DEP_PACKAGES: list[dict] = [
    {"key": "litellm", "label": "LiteLLM", "dist": "litellm"},
    {"key": "fastapi", "label": "FastAPI", "dist": "fastapi"},
    {"key": "uvicorn", "label": "Uvicorn", "dist": "uvicorn"},
    {"key": "httpx", "label": "HTTPX", "dist": "httpx"},
    {"key": "pyyaml", "label": "PyYAML", "dist": "PyYAML"},
]


# 在被探测解释器里运行，输出一行 JSON。
# 用 importlib.metadata 取已安装版本，缺包时置 None 而不是抛错。
_VERSION_PROBE = r"""
import json, sys
from importlib import metadata as md
res = {"python_version": sys.version.split()[0], "python_exe": sys.executable}
want = {
    "litellm": "litellm",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "httpx": "httpx",
    "pyyaml": "PyYAML",
    "pip": "pip",
}
for k, dist in want.items():
    try:
        res[k] = md.version(dist)
    except Exception:
        res[k] = None
print(json.dumps(res))
"""


def _probe_versions(python: str | None) -> dict:
    """在目标解释器里探测各依赖版本，返回 dict（失败时字段为 None）。"""

    empty = {
        "python_version": None,
        "python_exe": python,
        "litellm": None,
        "fastapi": None,
        "uvicorn": None,
        "httpx": None,
        "pyyaml": None,
        "pip": None,
    }

    if not python:
        return empty

    try:
        proc = _run(
            [python, "-c", _VERSION_PROBE],
            timeout=40,
        )
    except Exception:
        return empty

    if proc.returncode != 0:
        return empty

    raw = (proc.stdout or b"").strip()
    if not raw:
        return empty

    # 取最后一行（解释器可能有其它输出）
    line = raw.splitlines()[-1]

    try:
        data = json.loads(_decode_line(line))
    except Exception:
        return empty

    if not isinstance(data, dict):
        return empty

    merged = {**empty, **data}
    merged["python_exe"] = python
    return merged


_LITELLM_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?(?:[.\-][0-9A-Za-z.]+)?)")


def _litellm_cli_version(litellm_bin: str | None) -> str | None:
    """
    通过 litellm --version 获取版本。

    当 litellm 由 uv tool 独立安装（不在探针解释器里）时，
    importlib.metadata 取不到版本，这里用 CLI 兜底。
    """

    if not litellm_bin:
        return None

    try:
        proc = _run(
            [litellm_bin, "--version"],
            timeout=60,
        )
    except Exception:
        return None

    raw = (proc.stdout or b"") + b"\n" + (proc.stderr or b"")
    text = _decode_line(raw)

    m = _LITELLM_VERSION_RE.search(text)
    return m.group(1) if m else None


def _is_uv_tool(litellm_path: str | None) -> bool:
    """
    判断 litellm 是否由 uv tool 安装（独立于项目解释器）。

    uv tool 的两种典型路径：
      - 真正的 venv 脚本：...\\uv\\tools\\litellm\\Scripts\\litellm.exe
      - 暴露到 PATH 的 shim：~/.local/bin/litellm(.exe)
    """

    low = (litellm_path or "").replace("\\", "/").lower()
    if not low:
        return False

    if "/uv/tools/" in low:
        return True

    # uv tool 的 shim 默认落在 ~/.local/bin
    name = low.rsplit("/", 1)[-1]
    if name in ("litellm", "litellm.exe") and "/.local/bin/" in low:
        return True

    return False


def build_update_plan(python: str | None, litellm_path: str | None) -> list[list[str]]:
    """
    构造依赖升级命令序列。

    - Python 侧依赖（fastapi / uvicorn / httpx / pyyaml）统一走 pip。
    - litellm 若由 uv tool 安装则用 `uv tool upgrade`，否则并入 pip。
    """

    cmds: list[list[str]] = []

    uv_managed = _is_uv_tool(litellm_path)
    uv_exe = shutil.which("uv")

    py_pkgs = ["fastapi", "uvicorn", "httpx", "pyyaml"]
    if not uv_managed:
        py_pkgs.append("litellm")

    if python:
        cmds.append(
            [python, "-m", "pip", "install", "--upgrade", *py_pkgs]
        )

    if uv_managed:
        if uv_exe:
            cmds.append([uv_exe, "tool", "upgrade", "litellm"])
        elif python:
            # uv 不在 PATH 时退回 pip（可能装到项目解释器里，但至少能升级）
            cmds.append([python, "-m", "pip", "install", "--upgrade", "litellm"])

    return cmds


class UpdateRunner:
    """
    依赖升级任务执行器。

    升级耗时较长（pip 解析 + 下载），放到后台线程执行，
    输出实时写入独立的 LogStream("update")，前端用 SSE 订阅。
    """

    def __init__(self):
        self.log = LogStream("update")
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.running = False
        self.ok: bool | None = None
        self.commands: list[str] = []
        self.started_at: str | None = None
        self.finished_at: str | None = None

    def start(self, python: str | None, litellm_path: str | None) -> dict:
        with self._lock:
            if self.running:
                return {"ok": False, "message": "更新正在进行中，请稍候"}

            cmds = build_update_plan(python, litellm_path)
            if not cmds:
                return {
                    "ok": False,
                    "message": "未找到可用的 Python 或 litellm，无法执行更新",
                }

            self.running = True
            self.ok = None
            self.commands = [" ".join(c) for c in cmds]
            self.started_at = datetime.now().isoformat(timespec="seconds")
            self.finished_at = None
            self.log.clear()

            self._thread = threading.Thread(
                target=self._run_plan, args=(cmds,), name="update-runner", daemon=True
            )
            self._thread.start()

            return {"ok": True, "commands": self.commands}

    def _run_plan(self, cmds: list[list[str]]) -> None:
        overall_ok = True

        self.log.append("[gui] 开始检查并升级外部依赖 ...", level="cmd")

        env = {**os.environ}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

        creationflags = 0
        if os.name == "nt":
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW
            )

        for cmd in cmds:
            self.log.append(f"[gui] $ {' '.join(cmd)}", level="cmd")

            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(ROOT),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    bufsize=0,
                    creationflags=creationflags,
                )
            except Exception as e:
                self.log.append(f"[gui] 无法执行：{e}", level="error")
                overall_ok = False
                continue

            pending = b""
            try:
                while True:
                    chunk = proc.stdout.read(1024)
                    if not chunk:
                        break
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        self.log.append(_decode_line(line))
                if pending:
                    self.log.append(_decode_line(pending))
            except Exception as e:
                self.log.append(f"[gui] 读取输出异常：{e}", level="error")
            finally:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

            code = proc.wait()
            if code == 0:
                self.log.append("[gui] 该步骤完成 (exit 0)", level="ok")
            else:
                self.log.append(f"[gui] 该步骤失败 (exit {code})", level="error")
                overall_ok = False

        self.ok = overall_ok
        self.running = False
        self.finished_at = datetime.now().isoformat(timespec="seconds")

        self.log.append(
            "[gui] 全部完成。重启 LiteLLM / Proxy 后新版本生效。"
            if overall_ok
            else "[gui] 更新过程中出现错误，请查看上方日志。",
            level="ok" if overall_ok else "error",
        )

    def status(self) -> dict:
        return {
            "running": self.running,
            "ok": self.ok,
            "commands": self.commands,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


UPDATE_RUNNER = UpdateRunner()


# ============================================================
# 服务管理器
# ============================================================


class ManagedService:
    """
    一个被 GUI 托管的子进程服务。

    负责启动、读取输出、优雅停止。
    """

    def __init__(
        self,
        key: str,
        title: str,
        build_cmd: Callable[[], list[str]],
        build_env: Callable[[], dict],
        port: int,
        health_path: str,
    ):
        self.key = key
        self.title = title
        self.build_cmd = build_cmd
        self.build_env = build_env
        self.port = port
        self.health_path = health_path

        self.proc: subprocess.Popen | None = None
        self.log = LogStream(key)
        self._reader: threading.Thread | None = None
        self.started_at: float | None = None
        self.last_error: str | None = None
        # 是否由用户主动停止：主动停止导致的非 0 退出码不算错误，
        # 否则界面会在「已停止」旁边误报一个红色 exit code。
        self._stopping = False

    # ---------------- 状态 ----------------

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def uptime(self) -> int | None:
        if self.started_at is None or not self.running:
            return None
        return int(time.time() - self.started_at)

    def status(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "port": self.port,
            "running": self.running,
            "pid": self.pid,
            "listening": port_listening(self.port),
            "uptime": self.uptime(),
            "last_error": self.last_error,
        }

    # ---------------- 启停 ----------------

    def start(self) -> dict:
        """启动服务。已在运行则直接返回。"""

        if self.running:
            return self.status()

        self.log.clear()
        self.last_error = None
        self._stopping = False

        # 构造命令 / 环境也可能失败（例如缺少解释器、config.yaml 解析异常），
        # 这类错误要回填到 last_error 让界面提示，不能直接冒泡成 500。
        try:
            cmd = self.build_cmd()
            env = {**os.environ, **self.build_env()}
        except Exception as e:
            self.last_error = str(e)
            self.log.append(f"[gui] 启动失败：{e}", level="error")
            self.proc = None
            return self.status()

        # LiteLLM 会在启动时尝试从 GitHub 拉取 model cost map，
        # 无外网时会阻塞很久。强制走本地内置表。
        env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

        creationflags = 0
        if os.name == "nt":
            # CREATE_NEW_PROCESS_GROUP：便于停止时连带结束其子进程。
            # NO_WINDOW：子进程绝不附着到任何控制台，
            # 从根上杜绝多出一个命令行窗口。
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW

        self.log.append(f"[gui] $ {' '.join(cmd)}", level="cmd")

        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                # 不在此处指定 text/encoding：
                # litellm 的横幅含 GBK 编码的制表符（如 0xbb），
                # 若交给 TextIOWrapper 按 utf-8 解码会抛
                # UnicodeDecodeError 并直接杀死读取线程。
                # 改为读 bytes，再由 _decode_line 容错解码。
                bufsize=0,
                creationflags=creationflags,
            )
        except Exception as e:
            self.last_error = str(e)
            self.log.append(f"[gui] 启动失败：{e}", level="error")
            self.proc = None
            return self.status()

        self.started_at = time.time()
        self._reader = threading.Thread(
            target=self._pump_output,
            name=f"log-{self.key}",
            daemon=True,
        )
        self._reader.start()

        self.log.append(
            f"[gui] {self.title} 已启动 (PID {self.proc.pid}, "
            f"端口 {self.port})",
            level="ok",
        )
        return self.status()

    def _pump_output(self) -> None:
        """
        持续读取子进程输出，写入日志流。

        以字节为单位读取并按行切分，再用 _decode_line 容错解码：
        不同工具（litellm / python / uvicorn）在 Windows 上的输出
        编码不一致，逐行解码才能保证既不丢日志也不炸线程。
        """

        proc = self.proc
        if proc is None or proc.stdout is None:
            return

        pending = b""

        try:
            while True:
                chunk = proc.stdout.read(4096)

                if not chunk:
                    break

                pending += chunk

                # 按 \n 切行，最后一段可能不完整，留到下一轮
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    self.log.append(_decode_line(line))

            # 收尾：处理没有换行结尾的最后一行
            if pending:
                self.log.append(_decode_line(pending))

        except Exception as e:
            self.log.append(f"[gui] 读取输出异常：{e}", level="error")
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

            code = proc.poll()
            if code is not None:
                # 主动停止时进程也会以非 0 退出码结束（Windows 上常见 1），
                # 这属于正常行为，不能记成错误。
                stopping = self._stopping
                level = "ok" if (code == 0 or stopping) else "error"
                self.log.append(
                    f"[gui] {self.title} 已退出 (exit code {code})",
                    level=level,
                )
                if code != 0 and not stopping:
                    self.last_error = f"exit code {code}"

    def stop(self) -> dict:
        """优雅停止服务，超时后强制结束。"""

        proc = self.proc
        if proc is None:
            # 进程句柄丢了，但端口可能仍被占（GUI 重启过的情况）
            killed = kill_port_owner(self.port)
            if killed:
                self.log.append(
                    f"[gui] 清理残留端口占用进程：{killed}", level="warn"
                )
            self._stopping = True
            self.last_error = None
            return self.status()

        if proc.poll() is None:
            self._stopping = True
            self.log.append("[gui] 正在停止服务 ...", level="warn")
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=TERMINATE_GRACE)
                except subprocess.TimeoutExpired:
                    self.log.append(
                        "[gui] 优雅停止超时，强制结束", level="warn"
                    )
                    _run(
                        ["taskkill", "/PID", str(proc.pid), "/F", "/T"],
                        timeout=15,
                    )
            except Exception as e:
                self.log.append(f"[gui] 停止失败：{e}", level="error")

        # 兜底：确认端口释放
        if port_listening(self.port):
            kill_port_owner(self.port)

        self.proc = None
        self.started_at = None
        # 主动停止不是错误，清掉退出码以免界面误报
        self.last_error = None
        return self.status()


class ServiceManager:
    """GUI 的全部服务编排入口。"""

    def __init__(self):
        self.python = find_python()
        self.litellm = find_litellm()

        self.litellm_port = DEFAULT_LITELLM_PORT
        self.proxy_port = DEFAULT_PROXY_PORT

        self.litellm_svc = ManagedService(
            key="litellm",
            title="LiteLLM",
            build_cmd=self._litellm_cmd,
            build_env=lambda: {},
            port=self.litellm_port,
            health_path="/health/liveliness",
        )

        self.proxy_svc = ManagedService(
            key="proxy",
            title="Router Proxy",
            build_cmd=self._proxy_cmd,
            build_env=self._proxy_env,
            port=self.proxy_port,
            health_path="/health",
        )

    # ---------------- 命令构造 ----------------

    def _litellm_cmd(self) -> list[str]:
        # 启动当刻再扫一次：GUI 刚起来时环境可能正好不可用
        # （例如依赖升级进行中），用户重试时应当能自愈。
        if not self.litellm:
            self.litellm = find_litellm()
        if not self.litellm:
            raise RuntimeError(
                "找不到 litellm。请先执行：uv tool install 'litellm[proxy]'"
            )
        return [
            self.litellm,
            "--config",
            str(CONFIG_YML),
            "--port",
            str(self.litellm_port),
        ]

    def _proxy_cmd(self) -> list[str]:
        # 同上：避免因启动瞬间的探测结果把错误永久固化下来。
        if not self.python:
            self.python = find_python()
        if not self.python:
            detail = "\n".join(f"  - {d}" for d in LAST_PYTHON_DIAG) or "  - 无候选"
            raise RuntimeError(
                "找不到带 fastapi/httpx/uvicorn 的 Python 解释器。\n"
                f"已依次尝试：\n{detail}\n"
                "可重新运行首次安装脚本重建依赖后重试。"
            )
        return [self.python, str(PROXY_PY)]

    def _proxy_env(self) -> dict:
        return {
            "LITELLM_BASE": f"http://127.0.0.1:{self.litellm_port}",
            "LITELLM_API_KEY": self.master_key(),
            "PROXY_PORT": str(self.proxy_port),
            # 强制子进程以 UTF-8 写 stdout。
            #
            # 管道模式下 Python 默认按本地代码页（中文为 GBK）编码，
            # 而 router.py 的模型能力表会打印 emoji（👁 / 🔊），
            # GBK 无法表示，print 会抛 UnicodeEncodeError
            # 并直接中断 proxy 启动。
            #
            # 日志读取端 _decode_line 正是优先按 UTF-8 解码，
            # 因此这里指定 UTF-8 与读取端完全对应。
            "PYTHONIOENCODING": "utf-8",
        }

    def master_key(self) -> str:
        try:
            return get_general_settings().get(
                "master_key", DEFAULT_MASTER_KEY
            )
        except Exception:
            return DEFAULT_MASTER_KEY

    # ---------------- 依赖状态 ----------------

    def deps(self) -> dict:
        return {
            "python": self.python,
            "litellm": self.litellm,
            "root": str(ROOT),
            "config_exists": CONFIG_YML.exists(),
            "proxy_exists": PROXY_PY.exists(),
            "ok": bool(self.python and self.litellm),
        }

    def deps_versions(self) -> dict:
        """
        「设置」页用的依赖版本汇总。

        返回：解释器信息、各依赖版本、litellm 安装方式、更新任务状态。
        """

        if not self.python:
            # 设置页「刷新版本」时顺带重扫，环境恢复后无需重启 GUI
            self.python = find_python()

        data = _probe_versions(self.python)

        # litellm 版本：先看项目解释器，取不到再用 CLI 兜底（uv tool 场景）
        litellm_ver = data.get("litellm")
        if not litellm_ver:
            litellm_ver = _litellm_cli_version(self.litellm)

        packages = []
        for spec in DEP_PACKAGES:
            key = spec["key"]
            ver = litellm_ver if key == "litellm" else data.get(key)
            packages.append(
                {
                    "key": key,
                    "label": spec["label"],
                    "version": ver,
                    "installed": bool(ver),
                }
            )

        return {
            "root": str(ROOT),
            "python_version": data.get("python_version"),
            "python_exe": self.python or data.get("python_exe"),
            "pip_version": data.get("pip"),
            "packages": packages,
            "litellm_path": self.litellm,
            "litellm_uv_managed": _is_uv_tool(self.litellm),
            "uv_available": shutil.which("uv"),
            "update": UPDATE_RUNNER.status(),
        }

    # ---------------- 组合操作 ----------------

    def start_all(self) -> dict:
        """先起 LiteLLM，等待就绪后再起 Proxy。"""

        results: dict[str, Any] = {}

        results["litellm"] = self.litellm_svc.start()

        if not self._wait_health(self.litellm_svc, timeout=90):
            results["litellm_warning"] = (
                "LiteLLM 未在 90 秒内就绪，请查看其日志"
            )

        results["proxy"] = self.proxy_svc.start()

        if not self._wait_health(self.proxy_svc, timeout=60):
            results["proxy_warning"] = "Proxy 未在 60 秒内就绪"

        return results

    def stop_all(self) -> dict:
        return {
            "proxy": self.proxy_svc.stop(),
            "litellm": self.litellm_svc.stop(),
        }

    def restart_all(self) -> dict:
        self.stop_all()
        time.sleep(1.0)
        return self.start_all()

    def _wait_health(self, svc: ManagedService, timeout: int) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not svc.running:
                return False
            if self.check_health(svc):
                return True
            time.sleep(0.8)
        return self.check_health(svc)

    def check_health(self, svc: ManagedService) -> dict | bool:
        """对服务执行一次 HTTP 健康检查。"""

        import urllib.error
        import urllib.request

        url = f"http://127.0.0.1:{svc.port}{svc.health_path}"

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

        try:
            with opener.open(url, timeout=3) as resp:
                return 200 <= resp.status < 500
        except urllib.error.HTTPError as e:
            # 有 HTTP 响应即说明服务在跑
            return 200 <= e.code < 500
        except Exception:
            return False

    # ---------------- 状态聚合 ----------------

    def status(self) -> dict:
        return {
            "deps": self.deps(),
            "master_key": self.master_key(),
            "litellm": {
                **self.litellm_svc.status(),
                "healthy": self.check_health(self.litellm_svc)
                if self.litellm_svc.running
                else False,
            },
            "proxy": {
                **self.proxy_svc.status(),
                "healthy": self.check_health(self.proxy_svc)
                if self.proxy_svc.running
                else False,
            },
            "claude_code": self.claude_code_env(),
        }

    def claude_code_env(self) -> dict:
        """返回 Claude Code 需要设置的环境变量。"""

        return {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{self.proxy_port}",
            "ANTHROPIC_AUTH_TOKEN": "sk-anything",
            "ANTHROPIC_MODEL": "router",
        }

    def claude_settings_json(self) -> str:
        """
        生成可直接粘贴进 Claude Code settings.json 的片段。

        env 块是 Claude Code 官方支持的环境变量覆盖方式。
        """

        env = self.claude_code_env()
        payload = {"env": env}
        return json.dumps(payload, indent=2, ensure_ascii=False)


# ============================================================
# 全局单例
# ============================================================

MANAGER = ServiceManager()
