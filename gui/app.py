"""
AI Router GUI - HTTP API 服务

为前端界面提供 REST + SSE 接口：
  GET  /                       -> 前端页面
  GET  /api/status             -> 依赖 / 服务 / 健康 聚合状态
  POST /api/service/{key}/start
  POST /api/service/{key}/stop
  POST /api/service/start-all
  POST /api/service/stop-all
  POST /api/service/restart-all
  GET  /api/logs/{key}         -> 日志历史（?since=seq）
  GET  /api/logs/{key}/stream  -> SSE 实时日志
  POST /api/logs/{key}/clear
  GET  /api/models             -> 模型列表
  POST /api/models             -> 新增 / 更新
  DELETE /api/models/{index}   -> 删除
  GET  /api/settings           -> master_key 等
  POST /api/settings/master-key
  GET  /api/settings/language  -> 界面语言（zh / en）
  POST /api/settings/language
  GET  /api/usage              -> 最近请求 + 今日用量汇总
  GET  /api/deps/versions      -> 外部依赖版本信息
  POST /api/deps/update        -> 一键升级依赖（后台任务）
  GET  /api/deps/update/status -> 升级任务状态
  GET  /api/deps/update/logs   -> 升级日志历史
  GET  /api/deps/update/stream -> SSE 实时升级日志
  GET  /api/claude-code        -> Claude Code 配置片段

由 main.py 以 uvicorn 方式在本机端口启动。
"""

from __future__ import annotations

import asyncio
import json
import queue
import traceback
from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    StreamingResponse,
)

import backend
from backend import MANAGER

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"

app = FastAPI(title="AI Router GUI", version="1.0.0")

# 接口异常落盘位置（便于 pythonw 无控制台时排查）
API_ERROR_LOG = FRONTEND_DIR.parent.parent / "logs" / "gui_api_error.log"


@app.exception_handler(Exception)
async def _unhandled_exception(request, exc: Exception):
    """
    兜底异常处理。

    FastAPI 默认对未捕获异常只回 "Internal Server Error"，
    前端只能看到这句无信息的话。这里改为：
      1) 把异常类型与消息作为 detail 返回，界面直接显示真正原因；
      2) 把完整 traceback 追加到 logs/gui_api_error.log。
    """

    detail = f"{type(exc).__name__}: {exc}"

    tb = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )

    try:
        API_ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(API_ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
                f"{request.method} {request.url.path}\n{tb}\n"
            )
    except Exception:
        pass

    return JSONResponse(status_code=500, content={"detail": detail})


# ============================================================
# 静态页面
# ============================================================


@app.get("/")
async def index():
    if not INDEX_HTML.exists():
        raise HTTPException(500, f"前端文件缺失：{INDEX_HTML}")
    return FileResponse(str(INDEX_HTML))


# ============================================================
# 状态
# ============================================================


@app.get("/api/status")
async def get_status():
    # 状态查询涉及端口探测与健康检查，放到线程池避免阻塞事件循环
    return await asyncio.to_thread(MANAGER.status)


@app.get("/api/deps")
async def get_deps():
    return await asyncio.to_thread(MANAGER.deps)


# ============================================================
# 服务控制
# ============================================================


def _svc(key: str) -> backend.ManagedService:
    if key == "litellm":
        return MANAGER.litellm_svc
    if key == "proxy":
        return MANAGER.proxy_svc
    raise HTTPException(404, f"未知服务：{key}")


@app.post("/api/service/{key}/start")
async def service_start(key: str):
    svc = _svc(key)
    return await asyncio.to_thread(svc.start)


@app.post("/api/service/{key}/stop")
async def service_stop(key: str):
    svc = _svc(key)
    return await asyncio.to_thread(svc.stop)


@app.post("/api/service/start-all")
async def service_start_all():
    return await asyncio.to_thread(MANAGER.start_all)


@app.post("/api/service/stop-all")
async def service_stop_all():
    return await asyncio.to_thread(MANAGER.stop_all)


@app.post("/api/service/restart-all")
async def service_restart_all():
    return await asyncio.to_thread(MANAGER.restart_all)


# ============================================================
# 日志
# ============================================================


@app.get("/api/logs/{key}")
async def get_logs(key: str, since: int = 0):
    svc = _svc(key)
    return {"lines": svc.log.snapshot(since)}


@app.post("/api/logs/{key}/clear")
async def clear_logs(key: str):
    svc = _svc(key)
    svc.log.clear()
    return {"ok": True}


@app.get("/api/logs/{key}/stream")
async def stream_logs(key: str, since: int = 0):
    svc = _svc(key)
    return _sse_stream(svc.log, since)


def _sse_stream(logstream, since: int) -> StreamingResponse:
    """
    通用 SSE 日志流。

    用 asyncio.Queue 把同步的订阅回调桥接到异步生成器：
    订阅回调在工作线程被调用，通过 call_soon_threadsafe 投递到
    事件循环里的队列，生成器再逐个 yield 出去。
    """

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=2000)

    def on_log(entry: dict) -> None:
        try:
            loop.call_soon_threadsafe(q.put_nowait, entry)
        except Exception:
            pass

    unsubscribe = logstream.subscribe(on_log)

    async def gen():
        try:
            # 先补发历史
            for entry in logstream.snapshot(since):
                yield _sse(entry)

            while True:
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=20)
                    yield _sse(entry)
                except asyncio.TimeoutError:
                    # 心跳，防止中间层切断空闲连接
                    yield ": keep-alive\n\n"
        finally:
            unsubscribe()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _sse(entry: dict) -> str:
    return f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"


# ============================================================
# 模型配置
# ============================================================


@app.get("/api/models")
async def list_models():
    try:
        return {"models": await asyncio.to_thread(backend.get_models)}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/models")
async def save_model(payload: dict = Body(...)):
    try:
        model = await asyncio.to_thread(backend.upsert_model, payload)
        return {"ok": True, "model": model}
    except (ValueError, IndexError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/api/models/{index}")
async def remove_model(index: int):
    try:
        await asyncio.to_thread(backend.delete_model, index)
        return {"ok": True}
    except IndexError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/models/reorder")
async def reorder_model_list(payload: dict = Body(...)):
    """
    按给定顺序重排 model_list（GUI 拖放排序的落点）。

    body: {"order": ["easy", "easy-com", "balanced", ...]}
    顺序即调用优先级：同档位内先出现者优先。
    """

    order = (payload or {}).get("order")

    if not isinstance(order, list) or not all(
        isinstance(n, str) for n in order
    ):
        raise HTTPException(400, "order 必须是字符串列表")

    try:
        models = await asyncio.to_thread(backend.reorder_models, order)
        return {"ok": True, "models": models}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


# ============================================================
# 设置
# ============================================================


@app.get("/api/settings")
async def get_settings():
    try:
        data = await asyncio.to_thread(backend.get_general_settings)
        return {
            **data,
            "litellm_port": MANAGER.litellm_port,
            "proxy_port": MANAGER.proxy_port,
            "config_path": str(backend.CONFIG_YML),
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/settings/master-key")
async def set_master_key(payload: dict = Body(...)):
    key = (payload or {}).get("master_key", "")
    try:
        await asyncio.to_thread(backend.set_master_key, key)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/settings/language")
async def get_language():
    """界面语言（zh / en）。仅影响 GUI 文案，不参与路由。"""
    try:
        return {"language": await asyncio.to_thread(backend.get_language)}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/settings/language")
async def set_language(payload: dict = Body(...)):
    lang = (payload or {}).get("language", "")
    try:
        value = await asyncio.to_thread(backend.set_language, lang)
        return {"ok": True, "language": value}
    except Exception as e:
        raise HTTPException(500, str(e))


# ============================================================
# 用量统计
# ============================================================


@app.get("/api/usage")
async def get_usage(limit: int = 10):
    """最近请求 + 今日按模型的 token / 请求数汇总（读问答日志）。"""

    try:
        return await asyncio.to_thread(backend.get_usage_stats, limit)
    except Exception as e:
        raise HTTPException(500, str(e))


# ============================================================
# 外部依赖（版本 / 一键更新）
# ============================================================


@app.get("/api/deps/versions")
async def deps_versions():
    """返回解释器与各外部依赖的当前版本。"""

    try:
        return await asyncio.to_thread(MANAGER.deps_versions)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/deps/update")
async def deps_update():
    """启动后台依赖升级任务（异步，不阻塞）。"""

    def _start():
        return backend.UPDATE_RUNNER.start(MANAGER.python, MANAGER.litellm)

    try:
        res = await asyncio.to_thread(_start)
        if not res.get("ok"):
            raise HTTPException(400, res.get("message", "无法开始更新"))
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/deps/update/status")
async def deps_update_status():
    return backend.UPDATE_RUNNER.status()


@app.get("/api/deps/update/logs")
async def deps_update_logs(since: int = 0):
    return {"lines": backend.UPDATE_RUNNER.log.snapshot(since)}


@app.get("/api/deps/update/stream")
async def deps_update_stream(since: int = 0):
    return _sse_stream(backend.UPDATE_RUNNER.log, since)


# ============================================================
# 强制路由（force select）
# ============================================================


@app.get("/api/route-override")
async def get_route_override():
    try:
        return await asyncio.to_thread(backend.get_route_override)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/route-override")
async def set_route_override(payload: dict = Body(...)):
    """
    设置 / 清除强制路由。

    body: {"model": "strong"}  -> 强制使用 strong
          {"model": null}      -> 恢复智能路由
    """

    model = (payload or {}).get("model")

    if model is not None and not isinstance(model, str):
        raise HTTPException(400, "model 必须是字符串或 null")

    try:
        result = await asyncio.to_thread(backend.set_route_override, model)
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(500, str(e))


# ============================================================
# Claude Code 配置
# ============================================================


@app.get("/api/claude-code")
async def claude_code():
    return {
        "env": MANAGER.claude_code_env(),
        "settings_json": MANAGER.claude_settings_json(),
        "claude_dir": str(backend.claude_config_dir()),
    }


@app.post("/api/open-claude-dir")
async def open_claude_dir():
    """
    在资源管理器中打开 Claude Code 配置目录。

    目录不存在时先创建，保证「打开」不会失败。
    """

    import subprocess

    target = backend.claude_config_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer", str(target)])
        return {"ok": True, "path": str(target)}
    except Exception as e:
        raise HTTPException(500, f"无法打开目录：{e}")


@app.post("/api/shutdown")
async def shutdown():
    """
    停止全部子服务。

    由前端在用户点击「退出」或关闭窗口时调用（含 sendBeacon），
    确保 litellm / proxy 不会在界面关闭后变成孤儿进程。
    """

    result = await asyncio.to_thread(MANAGER.stop_all)
    return {"ok": True, "result": result}


@app.get("/api/ports")
async def get_ports():
    """返回当前实际使用的端口配置。"""

    return {
        "litellm": MANAGER.litellm_port,
        "proxy": MANAGER.proxy_port,
    }


@app.post("/api/ports")
async def set_ports(payload: dict = Body(...)):
    """
    修改端口。

    注意：端口是启动期参数，修改后需重启服务才生效。
    """

    llm = payload.get("litellm")
    px = payload.get("proxy")

    if llm is not None:
        llm = int(llm)
        if not (1024 <= llm <= 65535):
            raise HTTPException(400, "LiteLLM 端口需在 1024-65535 之间")
        MANAGER.litellm_port = llm
        MANAGER.litellm_svc.port = llm

    if px is not None:
        px = int(px)
        if not (1024 <= px <= 65535):
            raise HTTPException(400, "Proxy 端口需在 1024-65535 之间")
        MANAGER.proxy_port = px
        MANAGER.proxy_svc.port = px

    return {"ok": True, "litellm": MANAGER.litellm_port, "proxy": MANAGER.proxy_port}
