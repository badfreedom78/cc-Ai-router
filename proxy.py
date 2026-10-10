import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# 复用 router.py 里的判断逻辑
import router as router_module

# 模型健康管理（冷却 / 失败切换）
import failover


# ------------------------------------------------------------
# 上游 LiteLLM 地址
#
# 用 127.0.0.1 而不是 localhost：
# Windows 上 localhost 优先解析到 IPv6 ::1，
# 而 LiteLLM 默认只监听 IPv4，会连接失败。
# ------------------------------------------------------------

LITELLM_BASE = os.environ.get(
    "LITELLM_BASE",
    "http://127.0.0.1:4000",
)
LITELLM_MESSAGES_URL = f"{LITELLM_BASE}/v1/messages"

# 必须与 config.yaml 的 master_key 一致，
# 否则 LiteLLM 会返回误导性的 "No connected db."。
API_KEY = os.environ.get(
    "LITELLM_API_KEY",
    "sk-local-router-master-key",
)

# 代理监听端口
PROXY_PORT = int(os.environ.get("PROXY_PORT", "4001"))

# ------------------------------------------------------------
# 强制路由（force select）
#
# GUI 的「强制选择」会写一个 router_override.json。
# 只要文件里 force_model 有值，代理就跳过 Router 评分，
# 所有请求固定走该模型 —— 相当于临时关闭智能路由。
# 文件为空 / 不存在 / 解析失败，都视为「智能路由」。
# ------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))

OVERRIDE_PATH = os.environ.get(
    "ROUTER_OVERRIDE_PATH",
    os.path.join(HERE, "router_override.json"),
)

# ------------------------------------------------------------
# 问答日志
#
# 每处理一次请求追加一行 JSONL，用于后续统计 / 分析。
# 为了不让日志膨胀，提问内容只保留「几十字节」（默认 90 字节）。
# 日志写入永远不能影响主流程，失败只打印警告。
# ------------------------------------------------------------

QA_LOG_PATH = os.environ.get(
    "QA_LOG_PATH",
    os.path.join(HERE, "logs", "qa_log.jsonl"),
)

QA_LOG_ENABLED = os.environ.get(
    "QA_LOG_ENABLED", "1",
).strip().lower() not in ("0", "false", "no", "off", "")

QA_LOG_QUESTION_MAX_BYTES = int(
    os.environ.get("QA_LOG_QUESTION_MAX_BYTES", "90")
)

# 失败自动切换：单次请求最多尝试几个候选模型。
# 候选链按「本档全部 -> 相邻档」生成，通常 3-6 个，
# 上限防住极端配置下的长重试。
FAILOVER_MAX_ATTEMPTS = int(
    os.environ.get("ROUTER_FAILOVER_MAX_ATTEMPTS", "4")
)

# 设为 0 可完全关闭失败切换（回到旧的透传行为）
FAILOVER_ENABLED = (
    os.environ.get("ROUTER_FAILOVER", "1")
    .strip()
    .lower()
    not in ("0", "false", "no", "off", "")
)

# 从响应里提取 token 用量。
#
# 字段名带引号前缀是为了避开 cache_read_input_tokens /
# cache_creation_input_tokens 这类名字（它们前面是下划线，不会命中）。
_INPUT_TOKENS_RE = re.compile(r'"input_tokens"\s*:\s*(\d+)')
_OUTPUT_TOKENS_RE = re.compile(r'"output_tokens"\s*:\s*(\d+)')

# ------------------------------------------------------------
# 关键：禁用 httpx 读取 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY
#
# 开发机常常设置了系统级代理，
# 若不设 trust_env=False，httpx 会把发往 127.0.0.1:4000
# 的请求送到外部代理，返回 502 upstream connect failed。
# ------------------------------------------------------------

HTTP_TIMEOUT = httpx.Timeout(
    connect=15.0,
    read=1800.0,
    write=120.0,
    pool=15.0,
)

# 复用同一个 AsyncClient，避免每个请求都重建连接池。
client = httpx.AsyncClient(
    timeout=HTTP_TIMEOUT,
    trust_env=False,
    follow_redirects=False,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：关闭时释放 httpx 连接池。"""

    yield

    await client.aclose()


app = FastAPI(lifespan=lifespan)


def upstream_headers() -> dict[str, str]:
    """构造转发到 LiteLLM 所需的鉴权头。"""

    return {
        "Authorization": f"Bearer {API_KEY}",
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }


# ============================================================
# 强制路由：读取 GUI 写入的覆盖配置
# ============================================================

# 强制路由配置的 mtime 缓存。
# GUI 改动会以原子写替换文件 -> mtime 变化 -> 自动重读，
# 因此「即时生效」的特性不受影响；而绝大多数「文件没变」的请求
# 只需一次 getmtime 系统调用，省掉 open + read + json 解析。
_force_cache: dict = {"mtime": None, "value": None}


def read_force_model() -> str | None:
    """
    读取「强制指定模型」。

    返回：
        - 配置有效且有值  -> 模型别名（强制模式）
        - 文件不存在 / 空 / 解析失败 -> None（智能路由）

    每请求都会检查，但只在文件 mtime 变化时才真正重读，
    因此 GUI 改动依然即时生效，同时避免每请求都做文件 IO。
    """

    try:

        try:
            mtime = os.path.getmtime(OVERRIDE_PATH)
        except OSError:
            # 文件不存在
            _force_cache["mtime"] = None
            _force_cache["value"] = None
            return None

        if _force_cache["mtime"] == mtime:
            return _force_cache["value"]

        with open(OVERRIDE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        model = (data or {}).get("force_model")

        value = (
            model.strip()
            if isinstance(model, str) and model.strip()
            else None
        )

        _force_cache["mtime"] = mtime
        _force_cache["value"] = value

        return value

    except Exception as e:

        print(f"[Proxy] 读取强制模型配置失败，按智能路由处理：{e}")
        return None


# ============================================================
# 问答日志
# ============================================================

def _truncate_bytes(text: str, max_bytes: int) -> str:
    """
    按 UTF-8 字节数截断字符串。

    直接切片 bytes 可能切碎多字节字符，
    因此用 errors="ignore" 解码，丢掉被截断的半截字符。
    """

    raw = text.encode("utf-8")

    if len(raw) <= max_bytes:
        return text

    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _as_int(value) -> int | None:
    """安全地把值转成 int，失败返回 None。"""

    if value is None or isinstance(value, bool):
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_retry_after(value: str | None) -> float | None:
    """
    解析 Retry-After 头。支持秒数和 HTTP 日期两种格式，
    解析失败返回 None（不影响主流程）。
    """

    if not value:
        return None

    value = value.strip()

    try:
        return float(value)
    except ValueError:
        pass

    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            from datetime import timezone

            dt = dt.replace(tzinfo=timezone.utc)

        import time as _time

        remain = dt.timestamp() - _time.time()
        return remain if remain > 0 else None
    except Exception:
        return None


def extract_usage(payload: dict) -> tuple[int | None, int | None]:
    """
    从「非流式」的 Anthropic 响应里取出 (输入 tokens, 输出 tokens)。
    """

    usage = (payload or {}).get("usage")

    if not isinstance(usage, dict):
        return None, None

    return (
        _as_int(usage.get("input_tokens")),
        _as_int(usage.get("output_tokens")),
    )


def write_qa_log(
    *,
    question: str,
    model: str,
    forced: bool,
    vision: bool,
    tier: str | None = None,
    source: str | None = None,
    in_tok: int | None = None,
    out_tok: int | None = None,
    status: int | None = None,
    elapsed_ms: int | None = None,
    attempts: int = 1,
    failed_models: list[str] | None = None,
) -> None:
    """
    追加一条问答日志（JSONL，一行一条）。

    字段刻意保持精简，提问只存几十字节，
    保证长时间运行后日志文件也不会明显膨胀。
    除问答本身外，还记录难度档位与 token 用量，
    供 GUI 的用量面板统计；attempts/failed_models
    记录失败切换情况。
    """

    if not QA_LOG_ENABLED:
        return

    try:

        record = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "model": model,
            "tier": tier,
            "source": source,
            "forced": bool(forced),
            "vision": bool(vision),
            "in_tok": in_tok,
            "out_tok": out_tok,
            "status": status,
            "ms": elapsed_ms,
            "attempts": attempts,
            "failed": failed_models or [],
            "q": _truncate_bytes(
                (question or "").strip(),
                QA_LOG_QUESTION_MAX_BYTES,
            ),
        }

        parent = os.path.dirname(os.path.abspath(QA_LOG_PATH))

        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)

        with open(QA_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    except Exception as e:

        print(f"[Proxy] 写入问答日志失败：{e}")


# ============================================================
# 从 Anthropic 请求中提取用户文本
# ============================================================

def extract_user_text(messages: list) -> str:
    """
    从 Anthropic 格式的 messages 里，找到最后一条真正的用户文本。

    跳过：
    - tool_result 类型的消息（那是工具返回，不是用户输入）
    - 只有图片没有文本的消息

    返回：用户文本，如果找不到返回空字符串
    """

    for msg in reversed(messages):

        if msg.get("role") != "user":
            continue

        content = msg.get("content")

        # 字符串形式
        if isinstance(content, str):
            if content.strip():
                return content

        # 数组形式
        elif isinstance(content, list):

            texts = []
            has_tool_result = False

            for part in content:

                if not isinstance(part, dict):
                    continue

                ptype = part.get("type")

                if ptype == "text":
                    texts.append(part.get("text", ""))

                elif ptype == "tool_result":
                    has_tool_result = True

            # 有真实文本
            if texts:
                return "\n".join(texts)

            # 只有 tool_result，继续往上找
            if has_tool_result:
                continue

    return ""


def detect_vision_needed(messages: list) -> bool:
    """检测 Anthropic 格式的 messages 里是否包含图片"""
    for msg in messages:

        content = msg.get("content")

        if not isinstance(content, list):
            continue

        for part in content:

            if not isinstance(part, dict):
                continue

            if part.get("type") in ("image", "image_url"):
                return True

    return False


def detect_audio_needed(messages: list) -> bool:
    """检测 Anthropic 格式的 messages 里是否包含音频内容块"""
    for msg in messages:

        content = msg.get("content")

        if not isinstance(content, list):
            continue

        for part in content:

            if not isinstance(part, dict):
                continue

            if part.get("type") in ("audio", "input_audio"):
                return True

    return False


# ============================================================
# Anthropic /v1/messages 代理
# ============================================================

@app.post("/v1/messages")
async def proxy_messages(request: Request):

    started = time.perf_counter()

    body = await request.json()

    messages = body.get("messages", [])

    # --------------------------------------------------------
    # 1. 提取用户文本 + 检测多模态需求
    # --------------------------------------------------------

    user_text = extract_user_text(messages)

    vision_needed = detect_vision_needed(messages)

    audio_needed = detect_audio_needed(messages)

    # --------------------------------------------------------
    # 2. 强制路由优先
    #
    # GUI 的「强制选择」开启时，跳过 Router 评分，
    # 所有请求固定走指定模型。
    # --------------------------------------------------------

    force_model = read_force_model()

    score: dict = {}
    score_source = "route"

    if force_model:

        target_model = force_model
        score_source = "forced"

        print(
            f"[Proxy] 强制模式：跳过智能路由，固定使用 {target_model}"
        )

    elif not user_text:

        target_model = "balanced"
        score_source = "no_text"
        print(f"[Proxy] 无用户文本，直接使用 {target_model}")

    else:

        # ----------------------------------------------------
        # 3. 判断难度
        #
        # 规则优先：classifier 有把握时直接出结果，
        # **不调用 Router LLM**，省掉一次网络往返与 prefill；
        # 拿不准时才回落到 LLM。
        # ----------------------------------------------------

        try:

            score, score_source = router_module.route_score(
                user_text,
                [{"role": "user", "content": user_text}],
                has_history=len(messages) > 1,
                history_turns=len(messages),
            )

        except Exception as e:

            print(f"[Proxy] Router 判断失败：{e}，回退到 balanced")

            score = {}
            score_source = "fallback"
            target_model = "balanced"

        else:

            # ------------------------------------------------
            # 4. 选择模型
            # ------------------------------------------------

            actual_needs = {
                "vision": vision_needed,
                "audio": audio_needed,
            }

            target_model = router_module.select_model(
                score,
                actual_needs,
            )

    # --------------------------------------------------------
    # 5. 难度档位（仅用于统计展示，不影响实际路由）
    # --------------------------------------------------------

    tier: str | None = None

    if score:
        try:
            tier = router_module.select_tier(score)
        except Exception:
            tier = None

    # --------------------------------------------------------
    # 5b. 路由决策响应头（零成本可观测性）
    #
    # Claude Code 会忽略未知响应头；但用 curl / GUI 排查时
    # 一眼就能看到「这次走了哪个模型、什么档位、规则还是 LLM」。
    # --------------------------------------------------------

    route_headers = {
        "x-router-model": str(target_model),
        "x-router-tier": str(tier or "none"),
        "x-router-source": str(score_source),
        "x-router-elapsed-ms": str(
            int((time.perf_counter() - started) * 1000)
        ),
    }

    # --------------------------------------------------------
    # 6. 控制台日志
    # --------------------------------------------------------

    preview = user_text[:80].replace("\n", " ")

    print(
        f"[Proxy] -> {target_model}  "
        f"[{score_source}]  "
        f"(input: {preview!r})"
    )

    # --------------------------------------------------------
    # 7. 问答日志
    #
    # 推迟到「拿到上游响应」之后再写：只有那时才知道 token 用量。
    # finalize 保证只写一次（流式 / 非流式 / 报错路径都会走到）。
    # --------------------------------------------------------

    logged = False

    def finalize(
        *,
        status: int | None,
        in_tok: int | None,
        out_tok: int | None,
        attempts: int = 1,
        failed_models: list[str] | None = None,
        final_model: str | None = None,
    ) -> None:

        nonlocal logged

        if logged:
            return

        logged = True

        write_qa_log(
            question=user_text,
            model=final_model or target_model,
            forced=bool(force_model),
            vision=vision_needed,
            tier=tier,
            source=score_source,
            in_tok=in_tok,
            out_tok=out_tok,
            status=status,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            attempts=attempts,
            failed_models=failed_models,
        )

    # --------------------------------------------------------
    # 8. 生成候选模型链
    #
    # 这是「API 失效 / 限额到期自动切换」的核心：
    # 不再绑定单个模型，而是拿到一条按优先级排序的候选链。
    # 转发时逐个尝试，某个模型 4xx/5xx 就冷却它、换下一个，
    # 用户完全无感。
    # --------------------------------------------------------

    def build_candidates() -> list[str]:

        if not FAILOVER_ENABLED:
            return [target_model]

        # 强制模式：尊重用户指定的模型，
        # 但同样给它配同档位的替补（指定的模型挂了自动顶上）。
        if force_model:

            chain = [force_model]

            if router_module.MODEL_CAPS:
                fm_tier = router_module.tier_of_model(force_model)
                if fm_tier:
                    for name in router_module.find_capable_models(
                        fm_tier,
                        vision_needed,
                        audio_needed,
                    ):
                        if name != force_model and name not in chain:
                            chain.append(name)

            return chain

        # 能力表为空（LiteLLM 未起 / 拉取失败）：只能用 tier 名本身，
        # LiteLLM 会按模型组名做简单转发。
        if not router_module.MODEL_CAPS:
            return [target_model]

        require_vision = (
            vision_needed
            or bool(score.get("requires_vision", False))
        )
        require_audio = (
            audio_needed
            or bool(score.get("requires_audio", False))
        )

        base_tier = (
            tier
            or router_module.tier_of_model(target_model)
            or "balanced"
        )

        chain = router_module.candidate_chain_for_tier(
            base_tier,
            require_vision,
            require_audio,
        )

        return chain[:FAILOVER_MAX_ATTEMPTS] or [target_model]

    candidates = build_candidates()

    if len(candidates) > 1:
        print(
            f"[Proxy] 候选链({len(candidates)}): "
            f"{' -> '.join(candidates)}"
        )

    # --------------------------------------------------------
    # 9. 带失败切换的转发
    # --------------------------------------------------------

    stream = body.get("stream", False)

    failed_models: list[str] = []
    last_error_body: dict | None = None
    last_error_status: int = 500

    for attempt_index, model_name in enumerate(candidates):

        body["model"] = model_name

        # 连接层错误（LiteLLM 没起 / 网络断）不属于模型问题，
        # 重试别的模型也没用，直接透传给客户端。
        try:
            req = client.build_request(
                "POST",
                LITELLM_MESSAGES_URL,
                json=body,
                headers=upstream_headers(),
            )
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            print(f"[Proxy] 连接 LiteLLM 失败：{e}")
            finalize(
                status=502,
                in_tok=None,
                out_tok=None,
                attempts=attempt_index + 1,
                failed_models=failed_models,
                final_model=model_name,
            )
            return JSONResponse(
                content={
                    "type": "error",
                    "error": {
                        "type": "upstream_unreachable",
                        "message": f"Cannot reach LiteLLM at {LITELLM_BASE}: {e}",
                    },
                },
                status_code=502,
                headers=route_headers,
            )

        # ---------- 上游报错：标冷却 + 换下一个 ----------

        if resp.status_code >= 400:

            error_body = await resp.aread()
            await resp.aclose()

            last_error_body = _safe_json(error_body)
            last_error_status = resp.status_code

            # 读 Retry-After（429 常见），失败不影响主流程
            retry_after = _parse_retry_after(
                resp.headers.get("retry-after")
            )

            if FAILOVER_ENABLED:

                failover.mark_failure(
                    model_name,
                    status_code=resp.status_code,
                    reason=f"HTTP {resp.status_code}",
                    retry_after=retry_after,
                )

                failed_models.append(model_name)

                has_next = attempt_index + 1 < len(candidates)

                if has_next:
                    next_model = candidates[attempt_index + 1]
                    remain = failover.cooling_status().get(
                        model_name, 0
                    )
                    print(
                        f"[Proxy] {model_name} 报错 "
                        f"HTTP {resp.status_code}，冷却 "
                        f"{remain:.0f}s，切换到 {next_model}"
                    )
                    continue

            # 候选耗尽：把最后一次的错误透传出去
            finalize(
                status=resp.status_code,
                in_tok=None,
                out_tok=None,
                attempts=attempt_index + 1,
                failed_models=failed_models,
                final_model=model_name,
            )

            hdrs = dict(route_headers)
            if failed_models:
                hdrs["x-router-failed"] = ",".join(failed_models)

            return JSONResponse(
                content=last_error_body,
                status_code=resp.status_code,
                headers=hdrs,
            )

        # ---------- 成功 ----------

        failover.mark_success(model_name)

        if attempt_index > 0:
            print(
                f"[Proxy] 切换成功：{model_name} "
                f"(第 {attempt_index + 1} 个候选)"
            )
            route_headers["x-router-model"] = str(model_name)

        route_headers["x-router-attempts"] = str(attempt_index + 1)

        # ---------- 流式 ----------

        if stream:

            async def stream_gen():

                input_tokens: int | None = None
                output_tokens: int | None = None

                # 上一块结尾的若干字符，兜住「数字被切块切断」
                carry = ""

                try:

                    async for chunk in resp.aiter_bytes():
                        yield chunk

                        # 顺手解析 SSE 里的 usage
                        try:
                            text = carry + chunk.decode(
                                "utf-8", errors="ignore"
                            )
                        except Exception:
                            continue

                        carry = text[-64:]

                        m_in = _INPUT_TOKENS_RE.search(text)
                        if m_in:
                            input_tokens = int(m_in.group(1))

                        for m_out in _OUTPUT_TOKENS_RE.finditer(text):
                            output_tokens = int(m_out.group(1))

                finally:

                    finalize(
                        status=resp.status_code,
                        in_tok=input_tokens,
                        out_tok=output_tokens,
                        attempts=attempt_index + 1,
                        failed_models=failed_models or None,
                        final_model=model_name,
                    )

                    await resp.aclose()

            return StreamingResponse(
                stream_gen(),
                media_type="text/event-stream",
                headers=route_headers,
            )

        # ---------- 非流式 ----------

        raw = await resp.aread()
        await resp.aclose()

        payload = _safe_json(raw)

        in_tok, out_tok = extract_usage(
            payload if isinstance(payload, dict) else {}
        )

        finalize(
            status=resp.status_code,
            in_tok=in_tok,
            out_tok=out_tok,
            attempts=attempt_index + 1,
            failed_models=failed_models or None,
            final_model=model_name,
        )

        return JSONResponse(
            content=payload,
            status_code=resp.status_code,
            headers=route_headers,
        )

    # 理论上走不到（candidates 至少有一个元素）
    return JSONResponse(
        content=last_error_body
        or {
            "type": "error",
            "error": {"message": "no candidate model"},
        },
        status_code=last_error_status,
        headers=route_headers,
    )


def _safe_json(raw: bytes):
    """
    把上游返回的字节尽量解析为 JSON。

    上游出错时可能返回纯文本，此时包装成
    {"type": "error", "error": {...}} 结构，
    保证客户端始终能拿到合法 JSON。
    """

    import json

    if not raw:
        return {"type": "error", "error": {"message": "empty upstream response"}}

    try:

        return json.loads(raw.decode("utf-8"))

    except Exception:

        return {
            "type": "error",
            "error": {
                "type": "upstream_error",
                "message": raw.decode("utf-8", errors="replace"),
            },
        }


# ============================================================
# 健康检查
# ============================================================

@app.get("/health")
async def health():

    return {
        "status": "ok",
        "caps_loaded": len(router_module.MODEL_CAPS),
        "force_model": read_force_model(),
        "rule_first": bool(router_module.RULE_FIRST),
        "rule_threshold": router_module.RULE_CONFIDENCE_THRESHOLD,
        "failover_enabled": FAILOVER_ENABLED,
        "cooling_models": failover.cooling_status(),
    }


# ============================================================
# 启动
# ============================================================

def main():

    print()
    print("=" * 60)
    print("AI Router Proxy 启动中...")
    print("=" * 60)

    # 启动时拉取一次模型能力
    caps = router_module.fetch_model_capabilities()
    router_module.MODEL_CAPS = caps

    print()
    print("=" * 60)
    print(f"Proxy 监听: http://127.0.0.1:{PROXY_PORT}")
    print(f"问答日志  : {QA_LOG_PATH}")
    print(f"强制路由  : {OVERRIDE_PATH}")
    forced = read_force_model()
    if forced:
        print(f"  [当前] 强制使用 {forced}（智能路由已关闭）")
    else:
        print("  [当前] 智能路由")
    if router_module.RULE_FIRST:
        print(
            "  [轻量化] 规则优先开启：高置信度任务跳过 Router LLM "
            f"(阈值 {router_module.RULE_CONFIDENCE_THRESHOLD})"
        )
    else:
        print("  [轻量化] 规则优先已关闭：每个请求都调用 Router LLM")
    if FAILOVER_ENABLED:
        print(
            "  [高可用] 失败自动切换开启：API 报错时同档位换模型重试 "
            f"(单请求最多 {FAILOVER_MAX_ATTEMPTS} 个候选)"
        )
    else:
        print("  [高可用] 失败自动切换已关闭：错误直接透传")
    print("Claude Code 环境变量:")
    print(f"  ANTHROPIC_BASE_URL=http://127.0.0.1:{PROXY_PORT}")
    print("  ANTHROPIC_AUTH_TOKEN=sk-anything")
    print("  ANTHROPIC_MODEL=router")
    print("=" * 60)
    print()

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=PROXY_PORT,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
