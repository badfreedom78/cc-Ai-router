import argparse
import base64
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

# 规则优先分类器（第一级分类）。
# 缺失时自动退化为「每请求都调 Router LLM」的旧行为，不影响可用性。
try:
    import classifier
except Exception:  # pragma: no cover
    classifier = None

# 模型优先级：以 config.yaml 的 model_list 顺序为准
# （GUI 拖放排序直接决定同档位内的调用优先级）。
# 缺失时回退到下面的内置 MODEL_PRIORITY。
try:
    import model_priority
except Exception:  # pragma: no cover
    model_priority = None


# ============================================================
# stdio 加固
# ============================================================

def harden_stdio() -> None:
    """
    加固 stdout / stderr，让日志既能完整输出又不会中断进程。

    两个问题一起解决：

    1) 编码：
       Windows 上通过管道启动时（例如 GUI 拉起 proxy.py），
       Python 按本地代码页（中文为 GBK）编码 stdout。
       一旦 print 出现 GBK 无法表示的字符（emoji 等），
       就会抛 UnicodeEncodeError 并直接中断启动流程。
       这里只放宽 errors，不改变编码本身（避免中文变乱码），
       不可编码的字符降级为 '?' 而不是抛异常。

    2) 缓冲：
       输出被重定向到管道时，stdout 默认是块缓冲。
       启动横幅会一直卡在缓冲区，
       直到进程退出（或缓冲写满）才一次性刷出，
       表现为 GUI 日志面板只看到"已启动"、看不到任何启动信息。
       这里改为行缓冲，日志可实时输出。

    本函数在模块导入时即生效，
    所以 proxy.py（import router）也自动受保护。
    """

    for stream in (sys.stdout, sys.stderr):

        try:

            stream.reconfigure(
                errors="replace",
                line_buffering=True,
            )

        except Exception:

            # 流不支持 reconfigure（或已被替换）时忽略。
            pass


harden_stdio()


# ============================================================
# 配置
# ============================================================

# ------------------------------------------------------------
# 允许通过环境变量覆盖，避免硬编码与 config.yaml 不一致。
#
# 注意 LITELLM_BASE 默认用 127.0.0.1 而不是 localhost：
# 在 Windows 上 localhost 会优先解析到 IPv6 ::1，
# 而 LiteLLM 默认只监听 IPv4，容易造成连接失败或串到别的服务。
# ------------------------------------------------------------

LITELLM_BASE = os.environ.get(
    "LITELLM_BASE",
    "http://127.0.0.1:4000",
)

CHAT_COMPLETIONS_URL = (
    f"{LITELLM_BASE}/v1/chat/completions"
)

MODEL_INFO_URL = (
    f"{LITELLM_BASE}/model_group/info"
)

# 必须与 config.yaml 的 general_settings.master_key 一致。
# 否则 LiteLLM 会返回极具误导性的
# 400 {"error":{"message":"No connected db.", ...}}
API_KEY = os.environ.get(
    "LITELLM_API_KEY",
    "sk-local-router-master-key",
)

# ------------------------------------------------------------
# 代理绕行：开发机常设置 HTTP_PROXY/HTTPS_PROXY，
# 会让发往本机 LiteLLM 的请求被送到外部代理，
# 返回 502 upstream connect failed。
#
# 这里构造一个显式不使用代理的 opener，供 http_json 使用。
# ------------------------------------------------------------

DIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({})
)

ROUTER_MODEL = "router"

# Router LLM 自身的候选链。
#
# Router 用的也常是免费 API，同样会挂 / 限额。
# 第一个失败时自动换下一个（名字需在 LiteLLM 里存在，
# 不存在的会被跳过）。ROUTER_BACKUP_MODELS 设为空串可关闭。
ROUTER_BACKUP_MODELS = [
    m.strip()
    for m in os.environ.get(
        "ROUTER_BACKUP_MODELS",
        "router-back",
    ).split(",")
    if m.strip()
]

# 难度层级，从低到高
TIER_ORDER = [
    "easy",
    "balanced",
    "strong",
]

MODEL_PRIORITY = {
    # 兜底优先级：仅当 model_priority.py 无法解析 config.yaml 时使用。
    # 正常情况下以 config.yaml 的 model_list 顺序为准（GUI 拖放排序）。
    "balanced":       1,   # 默认模型，最优先
    "balanced-mid":   2,   # 备用强模型
    "balanced-weak": 3,   # 备用弱模型
    "easy": 1,
    "easy-com": 2,
    # 其他层级留空，按默认排序
    # "strong": 1,
}


def _priority_of(model_name: str) -> int:
    """
    取模型优先级：config.yaml 顺序优先，旧表兜底。

    config.yaml 里排在越前的模型优先级越高（数字越小）。
    未登记的模型给一个大数（排在已知模型之后），
    再按名称排序保证结果稳定。
    """

    if model_priority is not None:
        dynamic = model_priority.load_model_priority()
        if dynamic:
            return dynamic.get(model_name, 10000)

    return MODEL_PRIORITY.get(model_name, 100)

# 全局模型能力表
#
# {
#     "easy": {
#         "vision": True,
#         "audio": False
#     },
#     ...
# }
#
MODEL_CAPS: dict[str, dict[str, bool]] = {}

# 升级重试：最多尝试几次不同档位
MAX_ESCALATION_RETRIES = 2

# 路由日志
# 默认写到脚本同目录下，文件名固定为 router_log.jsonl，
# 每条记录一行 JSON，便于后续脚本解析和阈值校准。
DEFAULT_LOG_FILENAME = "router_log.jsonl"
LOG_TASK_PREVIEW_LIMIT = 500


# ------------------------------------------------------------
# 轻量化开关：两级分类
#
# 第一级 = classifier.py 的确定性关键词打分（纯 Python，内存可忽略）；
# 第二级 = Router LLM（网络往返 + 完整 prompt prefill，最贵）。
#
# 规则置信度 >= 阈值时，直接用第一级结果，**完全跳过 LLM 调用**，
# 这是弱机器上最划算的优化 —— 省掉一次网络往返和一次 prompt 预处理。
# 拿不准（边界 / 极短 follow-up / 无信号）时才回落到第二级。
#
# 设 ROUTER_RULE_FIRST=0 可退回旧行为（每请求都调 LLM）做对比。
# ------------------------------------------------------------

def _env_flag(name: str, default: bool = True) -> bool:
    """读取布尔型环境变量；未设置时返回默认值。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


RULE_FIRST = _env_flag("ROUTER_RULE_FIRST", True)

RULE_CONFIDENCE_THRESHOLD = float(
    os.environ.get("ROUTER_RULE_THRESHOLD", "0.7")
)

# Router LLM 的最大输出 token。
# 可调小以降低费用/时延；若 Router 是推理模型、思考链较长，需保留足够空间。
ROUTER_MAX_TOKENS = int(
    os.environ.get("ROUTER_MAX_TOKENS", "4000")
)

# 提示词外置：优先读该文件（若存在且非空），否则用内置 SYSTEM_PROMPT。
# 外置后可以不改代码就裁剪 prompt，从而减少每次 LLM 调用的输入 token。
PROMPT_PATH = os.environ.get(
    "ROUTER_PROMPT_PATH",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "router_prompt.md",
    ),
)


# ============================================================
# Router System Prompt
# ============================================================

SYSTEM_PROMPT = r"""
You are an AI task router.

Your only job is to analyze the user's task along these dimensions:

1. Task type
2. Execution difficulty
3. Reasoning complexity
4. Risk
5. Required context size
6. Tool-call requirements
7. Context sufficiency
8. Missing critical information
9. Whether retrieval is needed
10. Whether user clarification is needed
11. Whether vision capability is needed
12. Whether audio capability is needed

You MUST return exactly one valid JSON object.

Forbidden:
- Markdown
- ```json
- Explanatory text
- Any leading or trailing text
- Comments
- Multiple JSON objects

Return exactly one JSON object.

The format MUST be exactly:

{
  "task_type": "coding|research|writing|analysis|other",
  "complexity": 1,
  "reasoning": 1,
  "risk": 1,
  "context_requirement": 1,
  "tool_requirement": 1,
  "context_sufficiency": "sufficient|partial|insufficient",
  "missing_context": ["string"],
  "need_retrieval": false,
  "need_user_clarification": false,
  "requires_vision": false,
  "requires_audio": false
}

------------------------------------------------------------
Field descriptions
------------------------------------------------------------

task_type:

Must be one of:

coding
research
writing
analysis
other

------------------------------------------------------------

complexity:

Overall complexity of the task.

1-2:
Very simple.

Examples:
- Write a simple Python function
- Simple string processing
- Simple translation

3-4:
Simple task.

5-6:
Moderately complex task.

Examples:
- Modify a single Service
- Add a simple feature
- Analyze an ordinary bug

7-8:
Complex task.

Examples:
- Multi-file changes
- Moderately complex debugging
- Database and business logic interacting
- Moderately complex system design

9-10:
Very complex.

Examples:
- Architecture changes to a large project
- High-risk systems such as payments, trading, concurrency
- Diagnosing complex production issues
- Major changes spanning multiple systems

------------------------------------------------------------

reasoning:

How much complex reasoning is required.

1-2:
Essentially no reasoning needed.

3-4:
Simple logical analysis.

5-6:
Multi-step analysis required.

7-8:
Complex logical reasoning.

9-10:
Highly complex reasoning, concurrency analysis, system-level reasoning.

------------------------------------------------------------

risk:

The impact if the task is done incorrectly.

1-2:
Almost no risk.

3-4:
Low risk.

5-6:
Moderate risk.

7-8:
Relatively high risk.

9-10:
Extremely high risk.

Examples:
Payments, funds, database consistency, security and production systems
should generally raise the risk score.

------------------------------------------------------------

context_requirement:

How much context is theoretically required to complete the task.

This only describes the objective context demand of the task;
it does not directly determine the model tier.

1-2:
Only the current question itself.

3-4:
A small amount of context.

5-6:
Needs to understand a few files or one module.

7-8:
Needs to understand multiple modules.

9-10:
Needs to understand a large project, cross-module architecture,
or a large amount of historical context.

------------------------------------------------------------

tool_requirement:

How many tool operations are needed.

1-2:
Essentially no tools needed.

3-4:
A small number of tools may be needed.

5-6:
Needs to read files, search code, or make simple edits.

7-8:
Needs extensive file operations, testing and debugging.

9-10:
Needs many tool calls, running tests, multiple rounds of edits, verification.

------------------------------------------------------------

## Context sufficiency rules

You must distinguish between:

1. The intrinsic complexity of the task.
2. The amount of context required to complete the task.
3. Whether the context actually provided is sufficient.
4. Whether extra information must be obtained through tools.

context_sufficiency must be one of:

- sufficient
- partial
- insufficient

Decision principles:

- If the current message already contains all the information needed to
  complete the task, set it to sufficient.
- If part of the work can be done but files still need to be read, history
  searched, or other information obtained, set it to partial.
- If the missing information is a necessary prerequisite for the task and
  cannot be obtained through the available tools, set it to insufficient.

Do not treat insufficient context as high task complexity.

For example:

- A simple question missing a necessary piece of conversation history does
  not mean a strong model is needed.
- When code must be modified but the code was not provided, first consider
  whether the repository can be read.
- When the user says "continue with the previous plan", check whether the
  conversation history or a summary exists.
- If information can be obtained through tools, prefer requesting retrieval
  instead of asking the user to repeat it.
- If the task has multiple reasonable interpretations that would lead to
  different results, consider asking the user for clarification.

missing_context only lists the information actually needed to complete the
task that is currently missing.

need_retrieval indicates whether extra context should be obtained by reading
files, searching code, or searching history.

need_user_clarification indicates whether the user must supply additional
information — not merely because context is thin.

All fields must be judged from the messages actually visible right now and
from the available tools. Never assume you have seen history, code, or files
that were not provided.

If no tool is currently available to obtain the missing information,
need_retrieval MUST be false, and you should consider setting
need_user_clarification to true.

------------------------------------------------------------

requires_vision:

Set to true if the task requires understanding image content.

Examples:

- Screenshots
- UI screenshots
- Error screenshots
- Text inside images
- Charts
- Flowcharts
- Architecture diagrams
- Handwritten content
- Image recognition

If the image is only described in words and true visual understanding is not
required, it may be false.

------------------------------------------------------------

requires_audio:

Set to true if the task requires understanding audio content.

Examples:

- Speech
- Recordings
- Audio files
- Audio analysis
- Speech content recognition

------------------------------------------------------------

Important:

Even if the task is very simple, you must still strictly return the full JSON.

Return JSON only.
"""


# ============================================================
# 提示词加载（外置优先，懒加载 + 缓存）
# ============================================================

_PROMPT_CACHE: str | None = None


def load_system_prompt() -> str:
    """返回当前生效的 Router 系统提示词。

    优先读 PROMPT_PATH（默认 router_prompt.md）；文件不存在 / 为空 /
    读取失败时回退到内置 SYSTEM_PROMPT。

    结果只读一次并缓存，避免每个请求都读盘。
    """

    global _PROMPT_CACHE

    if _PROMPT_CACHE is not None:
        return _PROMPT_CACHE

    text = ""

    try:

        if PROMPT_PATH and os.path.exists(PROMPT_PATH):

            with open(
                PROMPT_PATH,
                "r",
                encoding="utf-8",
            ) as f:

                text = f.read()

    except Exception as e:

        print(
            f"[WARN] 读取外置 prompt 失败，改用内置：{e}"
        )

        text = ""

    if not text.strip():

        text = SYSTEM_PROMPT

    _PROMPT_CACHE = text

    return _PROMPT_CACHE


# ============================================================
# HTTP 工具
# ============================================================

def http_json(
    url: str,
    method: str = "GET",
    payload: dict | None = None,
    timeout: int = 30,
) -> dict:
    """
    发送 HTTP JSON 请求。
    """

    data = None

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
    }

    if payload is not None:
        data = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    # 使用显式禁用代理的 opener：
    # 由于目标是本机 LiteLLM，绝不能走 HTTP_PROXY，
    # 否则会被外部代理拦截并返回 502。
    with DIRECT_OPENER.open(
        request,
        timeout=timeout,
    ) as response:

        raw = response.read().decode("utf-8")

    return json.loads(raw)


# ============================================================
# 能力标记符号
# ============================================================

def capability_markers() -> tuple[str, str, str]:
    """
    选择「视觉 / 音频 / 空位」三个标记符号。

    中文 Windows 的终端与管道按 GBK 编码，
    无法表示 emoji，直接打印会抛 UnicodeEncodeError。

    这里先探测 stdout 编码：
      - 能表示 emoji   -> 返回 👁 / 🔊 / "  "
      - 不能（GBK 等） -> 降级为 V / A / "-"

    这样任何环境下日志都保持可读，且永不因编码中断。
    """

    encoding = (
        getattr(sys.stdout, "encoding", None)
        or "utf-8"
    )

    try:

        "👁🔊".encode(encoding)

    except (UnicodeEncodeError, LookupError):

        return "V", "A", "-"

    return "👁", "🔊", "  "


# ============================================================
# 从 LiteLLM 获取模型能力
# ============================================================

def fetch_model_capabilities() -> dict[str, dict[str, bool]]:
    """
    从 LiteLLM /model_group/info 获取模型能力。
    """

    try:

        data = http_json(
            MODEL_INFO_URL,
            method="GET",
            timeout=10,
        )

    except Exception as e:

        print(
            f"[WARN] 无法拉取模型能力信息：{e}"
        )

        print(
            "[WARN] 将继续运行，"
            "但多模态能力路由可能无法生效。"
        )

        return {}

    caps: dict[str, dict[str, bool]] = {}

    for item in data.get("data", []):

        name = item.get("model_group")

        if not name:
            continue

        caps[name] = {
            "vision": bool(
                item.get(
                    "supports_vision",
                    False,
                )
            ),
            "audio": bool(
                item.get(
                    "supports_audio_input",
                    False,
                )
            ),
        }

    print(
        f"[INFO] 已加载 "
        f"{len(caps)} 个模型的能力信息："
    )

    on_vision, on_audio, off = (
        capability_markers()
    )

    for name, cap in caps.items():

        vision = on_vision if cap["vision"] else off
        audio = on_audio if cap["audio"] else off

        print(
            f"  {vision} {audio} {name}"
        )

    return caps


# ============================================================
# 检测消息中的多模态内容
# ============================================================

def detect_modality_from_messages(
    messages: list[dict[str, Any]],
) -> dict[str, bool]:
    """
    从真实 messages 结构中检测多模态内容。

    这是「事实检测」。

    如果消息中真的存在 image/audio，
    那么无论 Router LLM 怎么判断，
    最终都必须认为需要对应能力。
    """

    needs = {
        "vision": False,
        "audio": False,
    }

    for message in messages:

        content = message.get("content")

        # 普通文本消息
        if not isinstance(content, list):
            continue

        for part in content:

            if not isinstance(part, dict):
                continue

            ptype = part.get(
                "type",
                "",
            )

            # OpenAI 风格
            if ptype in (
                "image_url",
                "image",
            ):
                needs["vision"] = True

            # 音频
            elif ptype in (
                "input_audio",
                "audio",
            ):
                needs["audio"] = True

    return needs


# ============================================================
# 图片转换为 OpenAI-compatible message
# ============================================================

def image_file_to_data_url(
    image_path: str,
) -> str:
    """
    将本地图片转换为 data URL。
    """

    if not os.path.exists(image_path):
        raise FileNotFoundError(
            f"图片不存在：{image_path}"
        )

    mime_type, _ = mimetypes.guess_type(
        image_path
    )

    if not mime_type:
        mime_type = "image/png"

    with open(
        image_path,
        "rb",
    ) as f:

        encoded = base64.b64encode(
            f.read()
        ).decode("ascii")

    return (
        f"data:{mime_type};base64,{encoded}"
    )


# ============================================================
# 构造用户消息
# ============================================================

def build_messages(
    task: str,
    history: list[dict[str, Any]] | None = None,
    image_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    构造最终发送给 Router / 模型的 messages。

    接受 history 参数，让 Router 和最终执行模型
    都能看到真实的多轮上下文，而不是只有当前这一句。

    history 应当由上游会话管理器传入，
    元素是 OpenAI 风格的 message dict。
    """

    messages: list[dict[str, Any]] = list(
        history or []
    )

    content: list[dict[str, Any]] = []

    # 文本
    content.append(
        {
            "type": "text",
            "text": task,
        }
    )

    # 图片
    for image_path in image_paths or []:

        data_url = image_file_to_data_url(
            image_path
        )

        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": data_url,
                },
            }
        )

    # 如果有图片，使用 list 形式；
    # 否则保持普通字符串形式（兼容性更好）
    if image_paths:

        messages.append(
            {
                "role": "user",
                "content": content,
            }
        )

    else:

        messages.append(
            {
                "role": "user",
                "content": task,
            }
        )

    return messages


def messages_contain_real_history(
    messages: list[dict[str, Any]],
) -> bool:
    """
    简单判断 messages 中是否包含真正的多轮上下文。

    用于提示 Router：当前看到的不是单轮问题。
    """

    return len(messages) > 1


# ============================================================
# Router LLM
# ============================================================

def _extract_first_json_object(
    text: str,
) -> str | None:
    """
    从任意文本中提取第一个完整的 JSON object。

    使用括号配对扫描而非正则以支持嵌套结构，
    并正确跳过字符串字面量中的花括号。

    Args:
        text: 可能混杂解释文字的模型输出。

    Returns:
        提取到的 JSON 字符串；找不到返回 None。
    """

    start = text.find("{")

    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):

        char = text[index]

        if in_string:

            if escaped:
                escaped = False

            elif char == "\\":
                escaped = True

            elif char == '"':
                in_string = False

            continue

        if char == '"':
            in_string = True

        elif char == "{":
            depth += 1

        elif char == "}":
            depth -= 1

            if depth == 0:
                return text[start:index + 1]

    return None


class _RouterHTTPError(Exception):
    """Router 请求的 HTTP 层错误（触发备用模型切换）。"""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _post_router_payload(payload: dict) -> dict:
    """
    发送 Router 请求；HTTP 层错误转成 _RouterHTTPError，
    供上层换备用 Router 模型重试。
    """

    try:

        return http_json(
            CHAT_COMPLETIONS_URL,
            method="POST",
            payload=payload,
            timeout=120,
        )

    except urllib.error.HTTPError as e:

        body = e.read().decode(
            "utf-8",
            errors="replace",
        )

        raise _RouterHTTPError(
            e.code,
            f"Router HTTP {e.code}: {body[:300]}",
        ) from e

    except Exception as e:

        # 连接层错误也换候选（Router 可能指向不同的 API 站点）
        raise _RouterHTTPError(
            0,
            f"Router 连接失败：{e}",
        ) from e


def call_router(
    messages: list[dict[str, Any]],
    summary: str | None = None,
) -> dict:
    """
    调用 Router 模型分析任务。

    messages 中应包含完整的历史上下文。
    summary 是上游维护的会话摘要 / 任务状态，
    会作为 system 补充信息告诉 Router 当前任务的背景。
    """

    # 在 system 末尾补充摘要，让 Router 看到二层上下文
    extra_system = ""

    if summary:

        extra_system = (
            "\n\n## 当前会话摘要（来自上游会话管理器）\n\n"
            f"{summary}\n\n"
            "请把摘要中的任务状态、已确认约束和未解决问题"
            "纳入评分。\n"
        )

    # 提示 Router 当前是否包含历史
    if messages_contain_real_history(messages):

        extra_system += (
            "\n## 注意\n\n"
            "当前 messages 包含真实的多轮历史。"
            "请结合历史消息判断任务上下文是否充分，"
            "不要假设存在未提供的对话。\n"
        )

    else:

        extra_system += (
            "\n## 注意\n\n"
            "当前 messages 只有当前这一条用户请求。"
            "不要假设存在未提供的历史对话。\n"
        )

    payload = {
        "model": ROUTER_MODEL,

        "messages": [
            {
                "role": "system",
                "content": load_system_prompt() + extra_system,
            },
            *messages,
        ],

        "response_format": {
            "type": "json_object",
        },

        "temperature": 0,

        # 默认 4000，给思考链留空间；可用 ROUTER_MAX_TOKENS 调整
        "max_tokens": ROUTER_MAX_TOKENS,
    }

    # --------------------------------------------------------
    # Router 自身的失败切换（同款免费 API，同样会限额/失效）
    #
    # 只有 HTTP 层错误（429/401/5xx/网络）才换备用模型；
    # 解析失败 / 空内容等「模型回了但质量差」的情况
    # 换模型也难救，交给上层 fallback_score 降智处理。
    # --------------------------------------------------------

    router_chain = [ROUTER_MODEL] + [
        m for m in ROUTER_BACKUP_MODELS
        if m != ROUTER_MODEL
    ]

    last_http_error: Exception | None = None

    for model_name in router_chain:

        payload["model"] = model_name

        try:

            result = _post_router_payload(payload)

        except _RouterHTTPError as e:

            last_http_error = e

            print(
                f"[WARN] Router 模型 {model_name} 请求失败"
                f"（HTTP {e.status_code}），"
                f"尝试下一个候选"
            )

            continue

        break

    else:

        raise RuntimeError(
            f"Router 全部候选失败：{last_http_error}"
        )

    # --------------------------------------------------------
    # 提取 content，加 reasoning_content 兜底
    # --------------------------------------------------------

    try:

        choice = result["choices"][0]
        message = choice["message"]

    except (KeyError, IndexError, TypeError) as e:

        raise RuntimeError(
            f"Router 返回格式异常：{result}"
        ) from e

    content = message.get("content")

    if not content:

        # 兜底 1：reasoning_content
        content = message.get("reasoning_content")

        if content:
            print(
                "[INFO] Router 只返回了 reasoning_content，"
                "尝试从中提取 JSON"
            )

    if not content:

        # 打印诊断信息
        finish_reason = choice.get("finish_reason")
        usage = result.get("usage")

        print(
            f"[DEBUG] Router 返回空内容："
            f"finish_reason={finish_reason}, "
            f"usage={usage}"
        )

        raise RuntimeError(
            "Router 没有返回任何内容"
        )

    # --------------------------------------------------------
    # 解析 JSON
    # --------------------------------------------------------

    score = None

    try:

        score = json.loads(content)

    except json.JSONDecodeError:

        # 兜底 2：从文本中提取第一个「完整」的 JSON 对象。
        #
        # 不能用 r"\{[^{}]*\}" 这种简单正则：
        # 它遇到嵌套对象（如 provider_specific_fields）
        # 或字符串里含花括号时会截断，导致解析失败。
        # 所以这里改用括号配对扫描。
        extracted = _extract_first_json_object(
            content
        )

        if extracted is not None:

            try:

                score = json.loads(extracted)

                print(
                    "[INFO] 从文本中提取到 JSON"
                )

            except json.JSONDecodeError:

                pass

    if score is None:

        raise RuntimeError(
            "Router 返回了无法解析的内容：\n"
            f"{content[:800]}"
        )

    validate_score(score)

    return score

# ============================================================
# Router 输出验证
# ============================================================

def validate_score(
    score: dict,
):
    """
    验证 Router 输出。
    """

    numeric_fields = [
        "complexity",
        "reasoning",
        "risk",
        "context_requirement",
        "tool_requirement",
    ]

    valid_task_types = {
        "coding",
        "research",
        "writing",
        "analysis",
        "other",
    }

    valid_sufficiency = {
        "sufficient",
        "partial",
        "insufficient",
    }

    # task_type

    task_type = score.get(
        "task_type"
    )

    if task_type not in valid_task_types:

        raise RuntimeError(
            "Router field task_type 非法："
            f"{task_type}"
        )

    # 数值字段

    for key in numeric_fields:

        if key not in score:

            raise RuntimeError(
                f"Router JSON missing field: {key}"
            )

        value = score[key]

        # bool 是 int 的子类，
        # 所以必须显式排除
        if isinstance(value, bool):

            raise RuntimeError(
                f"Router field {key} "
                f"must be integer: {value}"
            )

        if not isinstance(
            value,
            int,
        ):

            raise RuntimeError(
                f"Router field {key} "
                f"must be integer: {value}"
            )

        if not 1 <= value <= 10:

            raise RuntimeError(
                f"Router field {key} "
                f"out of range: {value}"
            )

    # context_sufficiency

    if "context_sufficiency" not in score:

        # 默认值：未知时按 partial 处理，
        # 提醒调用方需要谨慎
        score["context_sufficiency"] = "partial"

    elif score["context_sufficiency"] not in valid_sufficiency:

        raise RuntimeError(
            "Router field context_sufficiency 非法："
            f"{score['context_sufficiency']}"
        )

    # missing_context（list[str]）

    if "missing_context" not in score:

        score["missing_context"] = []

    elif not isinstance(score["missing_context"], list):

        raise RuntimeError(
            "Router field missing_context "
            f"must be list: {score['missing_context']}"
        )

    else:

        for item in score["missing_context"]:

            if not isinstance(item, str):

                raise RuntimeError(
                    "Router field missing_context items "
                    f"must be string: {item}"
                )

    # need_retrieval / need_user_clarification（bool）

    for key in (
        "need_retrieval",
        "need_user_clarification",
    ):

        if key not in score:

            score[key] = False

        elif not isinstance(
            score[key],
            bool,
        ):

            raise RuntimeError(
                f"Router field {key} "
                f"must be boolean: "
                f"{score[key]}"
            )

    # requires_vision / requires_audio（bool）

    for key in (
        "requires_vision",
        "requires_audio",
    ):

        if key not in score:

            score[key] = False

        elif not isinstance(
            score[key],
            bool,
        ):

            raise RuntimeError(
                f"Router field {key} "
                f"must be boolean: "
                f"{score[key]}"
            )


# ============================================================
# 两级分类：规则优先 + LLM 兜底
# ============================================================

def route_score(
    task_text: str,
    messages: list[dict[str, Any]],
    summary: str | None = None,
    has_history: bool | None = None,
    history_turns: int | None = None,
) -> tuple[dict, str]:
    """得到一次路由评分。

    返回 ``(score, source)``，其中 ``source`` 为：

    - ``"rule"`` : 规则优先命中，未调用 Router LLM（快、省）
    - ``"llm"``  : 规则置信度不足，交给 Router LLM 兜底

    ``messages`` 用于 LLM 兜底路径；``has_history`` / ``history_turns``
    供规则分类器参考上下午境规模，缺省时从 ``messages`` 推断
    （上游若传的是精简后的消息，可显式传入真实会话规模）。

    当 RULE_FIRST 关闭或 classifier 不可用时，行为与旧版一致（总是走 LLM）。
    """

    if has_history is None:
        has_history = len(messages) > 1

    if history_turns is None:
        history_turns = len(messages)

    if RULE_FIRST and classifier is not None and task_text:

        rule_result = None

        try:

            rule_result = classifier.classify(
                task_text,
                has_history=has_history,
                history_turns=history_turns,
            )

        except Exception as e:

            print(
                f"[WARN] 规则分类异常，回退 Router LLM：{e}"
            )

        else:

            if rule_result.confidence >= RULE_CONFIDENCE_THRESHOLD:

                print(
                    f"[INFO] 规则优先命中"
                    f"（conf={rule_result.confidence:.2f}）："
                    f"{rule_result.reason}"
                )

                if rule_result.matched:

                    print(
                        "[INFO] 命中信号："
                        + ", ".join(rule_result.matched[:6])
                    )

                return rule_result.score, "rule"

            print(
                f"[INFO] 规则置信度不足"
                f"（conf={rule_result.confidence:.2f} "
                f"< {RULE_CONFIDENCE_THRESHOLD}），"
                "交给 Router LLM 兜底"
            )

            # 记住低置信度的规则结果：Router LLM 若彻底挂掉，
            # 用它降级也远好于「所有任务全挤到 balanced」
            # ——至少保留了难度档位判断。

    try:

        score = call_router(messages, summary=summary)

    except Exception as e:

        if rule_result is not None:

            print(
                f"[WARN] Router LLM 不可用（{e}），"
                "降级使用低置信度规则结果"
            )

            return rule_result.score, "rule-degraded"

        raise

    return score, "llm"


# ============================================================
# 根据任务难度选择 Tier
# ============================================================

def select_tier(
    score: dict,
) -> str:
    """
    根据任务本身的复杂度、推理要求和风险
    选择基础模型档位。

    注意：上下文规模不直接决定模型档位。
    工具需求也不直接决定模型档位。
    它们影响的是上下文准备策略，
    而不是基础 tier。

    strong:
        risk >= 8
        complexity >= 8
        reasoning >= 8

    balanced:
        complexity >= 5
        reasoning >= 5

    otherwise easy
    """

    if (
        score.get("risk", 0) >= 8
        or score.get("complexity", 0) >= 8
        or score.get("reasoning", 0) >= 8
    ):

        return "strong"

    if (
        score.get("complexity", 0) >= 5
        or score.get("reasoning", 0) >= 5
    ):

        return "balanced"

    return "easy"


# ============================================================
# 上下文行动规划
# ============================================================

def plan_context(
    score: dict,
) -> str:
    """
    根据 context_sufficiency 与 need_retrieval /
    need_user_clarification 决定下一步上下文行动。

    返回值：

    - execute           : 上下文足够，直接执行
    - retrieve          : 应该检索补充上下文（读文件、查历史等）
    - clarify           : 需要先向用户追问
    - execute_with_caution : 没有检索/追问条件，谨慎执行
    """

    sufficiency = score.get(
        "context_sufficiency",
        "partial",
    )

    if sufficiency == "sufficient":

        return "execute"

    if score.get("need_retrieval", False):

        return "retrieve"

    if score.get("need_user_clarification", False):

        return "clarify"

    return "execute_with_caution"


# ============================================================
# 升级 Tier
# ============================================================

def escalate_tier(
    current_tier: str,
) -> str:
    """
    在 TIER_ORDER 中把当前档位向上一级升级。
    已经是最强档位则保持不变。
    """

    order = TIER_ORDER

    try:
        index = order.index(current_tier)
    except ValueError:
        return "balanced"

    return order[
        min(index + 1, len(order) - 1)
    ]


# ============================================================
# 判断模型属于哪个 Tier
# ============================================================

def belongs_to_tier(
    model_name: str,
    tier: str,
) -> bool:
    """
    支持：

    easy
    easy-vision

    balanced
    balanced-vision

    strong
    strong-vision
    """

    return (
        model_name == tier
        or model_name.startswith(
            f"{tier}-"
        )
    )


def tier_of_model(model_name: str) -> str | None:
    """根据模型名反查它属于哪个 tier（如 balanced-mid -> balanced）。"""

    for t in TIER_ORDER:
        if model_name == t or model_name.startswith(f"{t}-"):
            return t

    return None


# ============================================================
# 找具备指定能力的模型
# ============================================================

def find_capable_model(
    tier: str,
    require_vision: bool,
    require_audio: bool,
) -> str | None:
    """
    在指定 Tier 中寻找满足能力要求的模型。
    多个候选时，按 MODEL_PRIORITY 定义的优先级排序。
    未在优先级表里出现的模型，默认优先级 100。
    """

    candidates: list[tuple[int, str]] = []

    for name, capabilities in MODEL_CAPS.items():

        if not belongs_to_tier(name, tier):
            continue

        if require_vision and not capabilities.get("vision", False):
            continue

        if require_audio and not capabilities.get("audio", False):
            continue

        priority = _priority_of(name)
        candidates.append((priority, name))

    if not candidates:
        return None

    # 按 (priority, name) 升序：priority 小的优先
    candidates.sort(key=lambda x: (x[0], x[1]))

    return candidates[0][1]


# ============================================================
# 多候选模型选择（供 proxy 失败切换用）
# ============================================================

try:
    import failover
except Exception:  # pragma: no cover
    failover = None


def find_capable_models(
    tier: str,
    require_vision: bool,
    require_audio: bool,
    *,
    skip_cooling: bool = True,
) -> list[str]:
    """
    与 find_capable_model 同口径，但返回**全部**候选（按优先级排序）。

    proxy 在转发失败时按这个列表逐个换模型重试，
    实现「API 失效 / 限额到期时同档位自动切换」。

    skip_cooling=True 时跳过冷却中的模型（最近报错的）；
    若跳过后一个都不剩，则返回完整列表——
    「都试过但失败了」总好过「直接拒绝服务」。
    """

    candidates: list[tuple[int, str]] = []

    for name, capabilities in MODEL_CAPS.items():

        if not belongs_to_tier(name, tier):
            continue

        if require_vision and not capabilities.get("vision", False):
            continue

        if require_audio and not capabilities.get("audio", False):
            continue

        priority = _priority_of(name)
        candidates.append((priority, name))

    if not candidates:
        return []

    candidates.sort(key=lambda x: (x[0], x[1]))

    ordered = [name for _, name in candidates]

    if not skip_cooling or failover is None:
        return ordered

    # 过滤冷却中的模型
    fresh = [
        name for name in ordered
        if not failover.is_cooling(name)
    ]

    return fresh or ordered


def candidate_chain_for_tier(
    tier: str,
    require_vision: bool,
    require_audio: bool,
) -> list[str]:
    """
    生成完整的候选模型链（去重），供 proxy 依次尝试。

    顺序（针对「免费 API 限额」场景做了取舍）：
      1. 本档位的全部候选（跳过冷却）；
      2. 相邻档位的候选 —— balanced 优先降级到 easy
         （免费额度下换更便宜的模型零成本），
         easy 只能升到 balanced；
      3. strong 永远兜底（它是最后一道防线，除非已在 strong）。

    能力要求沿链保持：需要 vision 的请求不会被换到
    不支持 vision 的模型上。
    """

    chain: list[str] = []

    def _add(t: str) -> None:
        for name in find_capable_models(
            t,
            require_vision,
            require_audio,
        ):
            if name not in chain:
                chain.append(name)

    _add(tier)

    try:
        idx = TIER_ORDER.index(tier)
    except ValueError:
        idx = 0

    if tier == "balanced":
        # 限额场景：先降级（免费/便宜），再升级
        _add("easy")
        _add("strong")
    else:
        # easy 升 balanced；strong 已是顶档
        for neighbor in TIER_ORDER[idx + 1:]:
            _add(neighbor)
        if idx > 0:
            _add(TIER_ORDER[idx - 1])

    return chain

# ============================================================
# 最终模型选择（基于 Router 评分）
# ============================================================

def select_model(
    score: dict,
    actual_needs: dict[str, bool],
) -> str:
    """
    综合：
      1. 任务难度（select_tier）
      2. Router 判断的能力需求
      3. 消息结构检测出的真实能力需求
      4. 同层内优先级

    最终选择模型。

    如果当前 tier 找不到满足能力的模型，
    会逐级向上升级寻找。
    """

    # 第一步：任务难度
    tier = select_tier(score)

    # 第二步：合并能力需求（实际检测 OR router 判断）
    require_vision = (
        actual_needs.get("vision", False)
        or score.get("requires_vision", False)
    )
    require_audio = (
        actual_needs.get("audio", False)
        or score.get("requires_audio", False)
    )

    print()
    print("[INFO] Capability requirements:")
    print(f"  vision: {require_vision}")
    print(f"  audio : {require_audio}")

    # 能力表为空时无法路由
    if not MODEL_CAPS:
        print("[WARN] MODEL_CAPS 为空，无法进行能力路由。")
        return tier

    # 第三步：从当前 tier 开始，逐级向上找
    try:
        start_index = TIER_ORDER.index(tier)
    except ValueError:
        start_index = 0

    for candidate_tier in TIER_ORDER[start_index:]:

        candidate = find_capable_model(
            candidate_tier,
            require_vision,
            require_audio,
        )

        if candidate:
            if candidate_tier != tier:
                print(
                    f"[INFO] {tier} 层级没有满足能力要求的模型，"
                    f"升级到 {candidate_tier} 的 {candidate}"
                )
            else:
                print(f"[INFO] {tier} 层级选中：{candidate}")
            return candidate

    # 所有 tier 都不满足
    print("[WARN] 没有任何模型满足能力要求。")
    print(f"[WARN] 回退到原始 Tier：{tier}")
    return tier


# ============================================================
# 按指定 Tier 选模型（不重新计算 tier）
# ============================================================

def select_model_for_tier(
    tier: str,
    actual_needs: dict[str, bool],
) -> str:
    """
    在指定 Tier 中选择具备必要能力的模型。

    与 select_model 的区别：
    不会重新根据 score 算 tier，
    仅在给定 tier 上做能力匹配，
    必要时逐级向上升级。
    """

    require_vision = actual_needs.get("vision", False)
    require_audio = actual_needs.get("audio", False)

    if not MODEL_CAPS:
        return tier

    candidate = find_capable_model(
        tier,
        require_vision,
        require_audio,
    )

    if candidate:
        return candidate

    # 当前 tier 找不到，逐级向上找
    try:
        start_index = TIER_ORDER.index(tier)
    except ValueError:
        start_index = 0

    for candidate_tier in TIER_ORDER[start_index + 1:]:

        candidate = find_capable_model(
            candidate_tier,
            require_vision,
            require_audio,
        )

        if candidate:
            print(
                f"[INFO] {tier} 层级没有满足能力要求的模型，"
                f"升级到 {candidate_tier} 的 {candidate}"
            )
            return candidate

    return tier


# ============================================================
# 决定是否应该升级重试
# ============================================================

def should_escalate_on_error(
    error: Exception,
    score: dict,
    current_tier: str,
) -> bool:
    """
    根据异常类型和当前评分，判断是否应该升级 tier 重试。

    原则：
    - 网络 / 鉴权 / 限流 / 参数错误：不要升级，
      这不是模型能力问题。
    - 一般执行失败 / 输出质量不足：可考虑升级。
    """

    msg = str(error).lower()

    # 不应该触发升级的错误类型
    no_escalate_keywords = (
        "unauthorized",
        "401",
        "403",
        "forbidden",
        "rate limit",
        "429",
        "quota",
        "billing",
        "invalid api key",
        "context length",
        "context_length",
        "maximum context",
        "max_tokens",
        "invalid request",
        "400 ",
        "422",
        "bad request",
        "not found",
        "404",
        "connection",
        "timeout",
    )

    for kw in no_escalate_keywords:
        if kw in msg:
            return False

    # 如果本身已经是 strong，不需要升级
    if current_tier == "strong":
        return False

    # 高风险任务失败时，更倾向升级
    if score.get("risk", 0) >= 6:
        return True

    # 中等以上复杂度 / 推理要求失败时，倾向升级
    if (
        score.get("complexity", 0) >= 6
        or score.get("reasoning", 0) >= 6
    ):
        return True

    return False


# ============================================================
# 执行最终任务
# ============================================================

def call_model(
    model: str,
    messages: list[dict[str, Any]],
    timeout: int = 1800,
) -> str:
    """
    使用最终选中的模型执行任务。
    """

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 32000,
    }

    try:

        result = http_json(
            CHAT_COMPLETIONS_URL,
            method="POST",
            payload=payload,
            timeout=timeout,
        )

    except urllib.error.HTTPError as e:

        body = e.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"模型执行 HTTP {e.code}: {body}"
        )

    choice = result["choices"][0]

    message = choice["message"]

    content = message.get("content")

    # --------------------------------------------------------
    # 思考模型兜底
    #
    # deepseek-flash / glm 等推理模型在 max_tokens 较小时，
    # 可能把 token 全用在思考链上，导致 content=None，
    # 只有 reasoning_content。
    #
    # 这种情况不能直接把思考链当正文返回——那是模型的自言自语，
    # 不是给用户的答案。应当视为「正文缺失」。
    # --------------------------------------------------------

    if not content:

        reasoning = message.get("reasoning_content")

        finish_reason = choice.get("finish_reason")

        usage = result.get("usage") or {}

        detail = usage.get(
            "completion_tokens_details",
        ) or {}

        reasoning_tokens = detail.get("reasoning_tokens")

        # 明确区分「思考耗尽 token」与「真的没输出」
        if reasoning and finish_reason == "length":

            raise RuntimeError(
                "模型正文为空且 finish_reason=length："
                "推理模型的思考链耗尽了 max_tokens "
                f"(reasoning_tokens={reasoning_tokens})。"
                "请提高 max_tokens 或更换非推理模型。"
            )

        if reasoning:

            # 有思考但没正文，同样不可当作答案
            raise RuntimeError(
                "模型只返回了 reasoning_content，没有正文。"
                f"finish_reason={finish_reason}, "
                f"reasoning_tokens={reasoning_tokens}"
            )

        raise RuntimeError(
            "模型没有返回任何内容。"
            f"finish_reason={finish_reason}, usage={usage}"
        )

    return content


def execute_with_escalation(
    score: dict,
    actual_needs: dict[str, bool],
    messages: list[dict[str, Any]],
) -> tuple[str, str, int]:
    """
    执行任务，必要时升级 tier 重试。

    返回 (final_model, answer, attempts) 元组：
    - final_model : 最终实际使用的模型
    - answer      : 模型返回内容
    - attempts    : 总尝试次数（含最后一次成功的）
    """

    tier = select_tier(score)
    model = select_model_for_tier(tier, actual_needs)

    last_error: Exception | None = None

    for attempt in range(MAX_ESCALATION_RETRIES + 1):

        print()
        print(
            f"[INFO] 第 {attempt + 1} 次尝试："
            f"tier={tier}, model={model}"
        )

        try:

            answer = call_model(model, messages)
            return model, answer, attempt + 1

        except Exception as e:

            last_error = e

            print(
                f"[WARN] 模型执行失败：{e}"
            )

            if not should_escalate_on_error(
                e,
                score,
                tier,
            ):
                print(
                    "[WARN] 此错误不建议升级 tier，"
                    "直接抛出。"
                )
                raise

            next_tier = escalate_tier(tier)

            if next_tier == tier:
                print(
                    "[WARN] 已经是最强档位，"
                    "无法继续升级。"
                )
                raise

            tier = next_tier
            model = select_model_for_tier(
                tier,
                actual_needs,
            )

            print(
                f"[INFO] 升级到 tier={tier}, "
                f"model={model} 重试"
            )

    # 理论上走不到这里，但兜底
    if last_error:
        raise last_error

    raise RuntimeError(
        "执行失败但未捕获到具体异常"
    )


# ============================================================
# 回退评分（Router 失败时使用）
# ============================================================

def fallback_score(
    actual_needs: dict[str, bool],
    risk: int = 5,
) -> dict:
    """
    当 Router 调用失败时使用的兜底评分。

    设计原则：

    - 风险相关字段不能盲目设为高，
      应当保持中性，让回退决策可分析。
    - 必须显式包含上下文充分性相关字段，
      避免后续代码因字段缺失而崩溃。
    - 必须保留真实的多模态能力需求。
    """

    return {
        "task_type": "other",
        "complexity": 5,
        "reasoning": 5,
        "risk": risk,
        "context_requirement": 5,
        "tool_requirement": 5,
        "context_sufficiency": "partial",
        "missing_context": [],
        "need_retrieval": False,
        "need_user_clarification": False,
        "requires_vision": actual_needs.get(
            "vision", False
        ),
        "requires_audio": actual_needs.get(
            "audio", False
        ),
    }


# ============================================================
# 风险感知的回退模型选择
# ============================================================

def fallback_model(
    actual_needs: dict[str, bool],
    risk: int = 5,
) -> str:
    """
    Router 不可用时的回退模型。

    - 普通低风险任务：选择具备必要能力的 balanced 模型。
    - 高风险任务：直接回退到 strong，避免在风险未知时
      使用弱模型自动执行。
    - 所有候选都不满足能力要求：返回 None，
      由调用方决定是报错还是继续。
    """

    require_vision = actual_needs.get("vision", False)
    require_audio = actual_needs.get("audio", False)

    if not MODEL_CAPS:
        # 没有任何能力信息，按风险做最保守选择
        if risk >= 8:
            return "strong"
        return "balanced"

    # 高风险任务优先 strong
    preferred_tier = (
        "strong" if risk >= 8 else "balanced"
    )

    candidate = select_model_for_tier(
        preferred_tier,
        actual_needs,
    )

    # 如果在 preferred tier 找不到，
    # select_model_for_tier 会向上找 strong，
    # 但不会向下找 easy。
    # 这里再补一个向下尝试，避免所有 strong/balanced
    # 都不满足能力要求时无路可走。
    if (
        candidate == preferred_tier
        and not find_capable_model(
            preferred_tier,
            require_vision,
            require_audio,
        )
    ):

        for tier in TIER_ORDER:

            if tier == preferred_tier:
                continue

            found = find_capable_model(
                tier,
                require_vision,
                require_audio,
            )

            if found:
                print(
                    f"[INFO] 回退时在 {tier} 找到满足能力的模型 {found}"
                )
                return found

        print(
            "[ERROR] 所有候选模型都不满足必要能力要求，"
            "无法安全回退。"
        )
        return None  # type: ignore[return-value]

    return candidate


# ============================================================
# 打印分析结果
# ============================================================

def print_analysis(
    task: str,
    score: dict,
    actual_needs: dict[str, bool],
    model: str,
    context_action: str,
    tier: str,
    history: list[dict[str, Any]] | None,
    summary: str | None,
):
    print()
    print("=" * 60)

    print("Task:")
    print(task)

    print()
    print(f"History turns: {len(history or [])}")
    print(f"Summary present: {bool(summary)}")

    print()
    print("Router Analysis:")

    print(
        json.dumps(
            score,
            ensure_ascii=False,
            indent=2,
        )
    )

    print()
    print("Detected Message Modality:")

    print(
        json.dumps(
            actual_needs,
            ensure_ascii=False,
            indent=2,
        )
    )

    print()
    print(f"Base tier: {tier}")
    print(f"Context action: {context_action}")
    print(f"Selected model: {model}")

    print("=" * 60)


# ============================================================
# 路由日志
# ============================================================

def resolve_log_path(
    log_file: str | None,
) -> str:
    """
    决定日志写入路径：

    - 用户通过 --log-file 指定：使用用户路径
    - 未指定：写到 router.py 同目录下的
      DEFAULT_LOG_FILENAME
    """

    if log_file:
        return log_file

    here = os.path.dirname(
        os.path.abspath(__file__)
    )

    return os.path.join(
        here,
        DEFAULT_LOG_FILENAME,
    )


def build_route_record(
    *,
    task: str,
    history: list[dict[str, Any]] | None,
    summary: str | None,
    score: dict,
    actual_needs: dict[str, bool],
    tier: str,
    context_action: str,
    initial_model: str,
    final_model: str | None,
    attempts: int,
    success: bool,
    error: str | None,
    dry_run: bool,
    forced: bool,
    score_source: str = "llm",
) -> dict:
    """
    构造一条路由决策日志。

    每条记录一行 JSON，方便后续脚本解析和阈值校准。
    """

    task_text = task or ""

    if len(task_text) > LOG_TASK_PREVIEW_LIMIT:
        task_preview = (
            task_text[:LOG_TASK_PREVIEW_LIMIT]
            + "..."
        )
    else:
        task_preview = task_text

    return {
        "timestamp": datetime.now().isoformat(
            timespec="seconds"
        ),
        "dry_run": bool(dry_run),
        "forced": bool(forced),
        "score_source": score_source,
        "success": bool(success),
        "task_length": len(task_text),
        "task_preview": task_preview,
        "history_turns": len(history or []),
        "summary_present": bool(summary),
        "score": score,
        "actual_needs": actual_needs,
        "base_tier": tier,
        "context_action": context_action,
        "initial_model": initial_model,
        "final_model": final_model,
        "escalated": (
            final_model is not None
            and final_model != initial_model
        ),
        "attempts": attempts,
        "error": error,
    }


def write_route_log(
    record: dict,
    log_path: str | None = None,
) -> None:
    """
    把一条记录追加写入 jsonl 文件。

    失败时只打印警告，不抛异常，避免日志失败影响主流程。
    """

    if not log_path:
        return

    try:

        # 确保父目录存在
        parent = os.path.dirname(
            os.path.abspath(log_path)
        )

        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)

        line = (
            json.dumps(
                record,
                ensure_ascii=False,
            )
            + "\n"
        )

        with open(
            log_path,
            "a",
            encoding="utf-8",
        ) as f:
            f.write(line)

    except Exception as e:

        print(
            f"[WARN] 写入路由日志失败：{e}"
        )


# ============================================================
# 加载历史会话
# ============================================================

def load_history_from_file(
    path: str,
) -> list[dict[str, Any]]:
    """
    从 JSON 文件加载历史消息。

    文件格式：

    [
      {"role": "user", "content": "..."},
      {"role": "assistant", "content": "..."},
      ...
    ]

    或

    {"messages": [...]}
    """

    if not os.path.exists(path):
        raise FileNotFoundError(
            f"历史文件不存在：{path}"
        )

    with open(
        path,
        "r",
        encoding="utf-8",
    ) as f:
        data = json.load(f)

    if isinstance(data, list):
        return data

    if isinstance(data, dict) and isinstance(
        data.get("messages"),
        list,
    ):
        return data["messages"]

    raise RuntimeError(
        "历史文件格式不正确："
        "应为 list 或 {messages: [...]} 结构"
    )


# ============================================================
# CLI
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "AI Task Router - "
            "根据任务难度和模型能力自动选择模型"
        )
    )

    parser.add_argument(
        "task",
        nargs="*",
        help="任务描述",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只判断模型，不执行任务",
    )

    parser.add_argument(
        "-m",
        "--model",
        dest="force_model",
        help="强制指定模型，跳过自动路由",
    )

    parser.add_argument(
        "--image",
        action="append",
        default=[],
        help=(
            "附加图片路径。"
            "可以多次使用 --image"
        ),
    )

    parser.add_argument(
        "--history-file",
        dest="history_file",
        default=None,
        help=(
            "包含历史消息的 JSON 文件路径。"
            "文件格式见 load_history_from_file。"
        ),
    )

    parser.add_argument(
        "--summary",
        dest="summary",
        default=None,
        help=(
            "上游会话管理器维护的会话摘要 / 任务状态。"
            "会作为二层上下文传给 Router。"
        ),
    )

    parser.add_argument(
        "--skip-context-warning",
        action="store_true",
        help=(
            "即使 context_sufficiency 不是 sufficient，"
            "也不打印警告，继续执行。"
        ),
    )

    parser.add_argument(
        "--no-log",
        action="store_true",
        help="关闭路由决策日志（默认写入 jsonl 日志）。",
    )

    parser.add_argument(
        "--log-file",
        dest="log_file",
        default=None,
        help=(
            "路由决策日志路径。"
            "默认写到 router.py 同目录下的 "
            f"{DEFAULT_LOG_FILENAME}。"
        ),
    )

    parser.add_argument(
        "--no-rule-first",
        dest="no_rule_first",
        action="store_true",
        help=(
            "关闭规则优先分类，每个请求都调用 Router LLM。"
            "等价于设置环境变量 ROUTER_RULE_FIRST=0。"
        ),
    )

    parser.add_argument(
        "--dump-prompt",
        action="store_true",
        help="把当前生效的 Router 提示词打印到标准输出后退出。",
    )

    return parser.parse_args()


# ============================================================
# Main
# ============================================================

def main():

    global MODEL_CAPS, RULE_FIRST

    args = parse_args()

    # --------------------------------------------------------
    # 打印当前生效的提示词后退出
    # --------------------------------------------------------

    if args.dump_prompt:

        print(load_system_prompt())
        return

    # --------------------------------------------------------
    # CLI 覆盖规则优先开关
    # --------------------------------------------------------

    if args.no_rule_first:
        RULE_FIRST = False

    # --------------------------------------------------------
    # 日志路径
    # --------------------------------------------------------

    log_path: str | None = None

    if not args.no_log:

        log_path = resolve_log_path(
            args.log_file
        )

        print(
            f"[INFO] 路由日志将写入：{log_path}"
        )

    # --------------------------------------------------------
    # 检查任务
    # --------------------------------------------------------

    if not args.task:

        print(
            'Usage: python router.py '
            '[--dry-run] '
            '[-m model] '
            '[--image xxx.png] '
            '[--history-file hist.json] '
            '[--summary "..."] '
            '"你的任务"'
        )

        sys.exit(1)

    task = " ".join(
        args.task
    )

    # --------------------------------------------------------
    # 加载历史（可选）
    # --------------------------------------------------------

    history: list[dict[str, Any]] = []

    if args.history_file:

        try:

            history = load_history_from_file(
                args.history_file
            )

            print(
                f"[INFO] 从 {args.history_file} "
                f"加载了 {len(history)} 条历史消息"
            )

        except Exception as e:

            print(
                f"[ERROR] 加载历史文件失败：{e}"
            )
            sys.exit(1)

    # --------------------------------------------------------
    # 加载模型能力
    # --------------------------------------------------------

    MODEL_CAPS = (
        fetch_model_capabilities()
    )

    # --------------------------------------------------------
    # 构造 messages（带 history）
    # --------------------------------------------------------

    try:

        messages = build_messages(
            task,
            history=history,
            image_paths=args.image,
        )

    except Exception as e:

        print(
            f"[ERROR] 构造消息失败：{e}"
        )
        sys.exit(1)

    # --------------------------------------------------------
    # 检测真实多模态内容
    # --------------------------------------------------------

    actual_needs = (
        detect_modality_from_messages(
            messages
        )
    )

    print()
    print(
        "[INFO] Message modality detection:"
    )

    print(
        json.dumps(
            actual_needs,
            ensure_ascii=False,
        )
    )

    # --------------------------------------------------------
    # 强制模型
    # --------------------------------------------------------

    if args.force_model:

        model = args.force_model

        score = fallback_score(actual_needs, risk=5)

        tier = "forced"

        context_action = "forced"

        score_source = "forced"

        print()
        print(
            f"[INFO] 强制使用模型：{model}"
        )

    else:

        # ----------------------------------------------------
        # Router 分析（规则优先，必要时 LLM 兜底）
        # ----------------------------------------------------

        print()
        print(
            "[INFO] 正在判断任务难度..."
        )

        try:

            score, score_source = route_score(
                task,
                messages,
                summary=args.summary,
            )

        except Exception as e:

            print(
                f"[ERROR] Router 判断失败：{e}"
            )

            # --------------------------------------------
            # 风险感知的回退
            # --------------------------------------------

            score = fallback_score(actual_needs, risk=5)

            score_source = "fallback"

            model = fallback_model(
                actual_needs,
                risk=score.get("risk", 5),
            )

            if model is None:

                print(
                    "[ERROR] 没有任何模型能满足必要能力要求，"
                    "请检查 LiteLLM 模型配置。"
                )

                # 写一条失败日志，便于排查
                early_record = build_route_record(
                    task=task,
                    history=history,
                    summary=args.summary,
                    score=score,
                    actual_needs=actual_needs,
                    tier=select_tier(score),
                    context_action=plan_context(score),
                    initial_model="(none)",
                    final_model=None,
                    attempts=0,
                    success=False,
                    error=(
                        "router_failed_and_no_capable_model: "
                        f"{e}"
                    ),
                    dry_run=args.dry_run,
                    forced=bool(args.force_model),
                    score_source=score_source,
                )

                write_route_log(
                    early_record,
                    log_path,
                )

                sys.exit(1)

            tier = select_tier(score)
            context_action = plan_context(score)

        else:

            tier = select_tier(score)

            context_action = plan_context(score)

            # --------------------------------------------
            # 上下文行动提示
            # --------------------------------------------

            print()
            print(
                f"[INFO] 上下文行动：{context_action}"
            )

            if (
                context_action == "clarify"
                and not args.skip_context_warning
            ):

                missing = score.get(
                    "missing_context",
                    [],
                )

                print(
                    "[WARN] Router 判定需要先向用户澄清："
                )

                for item in missing:
                    print(f"  - {item}")

                print(
                    "[WARN] CLI 默认仍会继续执行。"
                    "如需停止，请使用 --skip-context-warning"
                    " 反过来：或自行处理。"
                )

            elif (
                context_action == "retrieve"
                and not args.skip_context_warning
            ):

                print(
                    "[WARN] Router 判定需要先检索上下文"
                    "（读文件 / 查历史）。"
                )
                print(
                    "[WARN] CLI 默认仍会继续执行，"
                    "请确认上游已经把所需上下文传进来。"
                )

            elif (
                context_action == "execute_with_caution"
                and not args.skip_context_warning
            ):

                print(
                    "[WARN] Router 判定上下文可能不足，"
                    "但又无法检索或澄清，将谨慎执行。"
                )

            # --------------------------------------------
            # 最终模型选择
            # --------------------------------------------

            model = select_model_for_tier(
                tier,
                actual_needs,
            )

    # --------------------------------------------------------
    # 输出分析
    # --------------------------------------------------------

    print_analysis(
        task,
        score,
        actual_needs,
        model,
        context_action,
        tier,
        history,
        args.summary,
    )

    # --------------------------------------------------------
    # 构造初始日志记录
    # --------------------------------------------------------

    record = build_route_record(
        task=task,
        history=history,
        summary=args.summary,
        score=score,
        actual_needs=actual_needs,
        tier=tier,
        context_action=context_action,
        initial_model=model,
        final_model=None,
        attempts=0,
        success=False,
        error=None,
        dry_run=args.dry_run,
        forced=bool(args.force_model),
        score_source=score_source,
    )

    # --------------------------------------------------------
    # Dry Run
    # --------------------------------------------------------

    if args.dry_run:

        # dry-run 也写日志：用于离线分析「选了什么模型」
        record["error"] = "dry-run"

        write_route_log(record, log_path)

        print()
        print(
            "[DRY-RUN] "
            "已跳过模型执行。"
        )

        return

    # --------------------------------------------------------
    # 执行任务（带升级重试）
    # --------------------------------------------------------

    print()
    print(
        "Executing task..."
    )

    try:

        (
            final_model,
            answer,
            attempts,
        ) = execute_with_escalation(
            score,
            actual_needs,
            messages,
        )

    except Exception as e:

        print()
        print(
            f"Execution error: {e}"
        )

        record["success"] = False
        record["error"] = str(e)

        write_route_log(record, log_path)

        sys.exit(1)

    record["final_model"] = final_model
    record["attempts"] = attempts
    record["success"] = True
    record["escalated"] = (
        final_model != record["initial_model"]
    )

    write_route_log(record, log_path)

    if final_model != model:
        print()
        print(
            f"[INFO] 执行过程中从 {model} 升级到 {final_model}"
        )

    # --------------------------------------------------------
    # 输出
    # --------------------------------------------------------

    print()
    print(
        "========== RESULT =========="
    )

    print(answer)


# ============================================================
# Entry
# ============================================================

if __name__ == "__main__":
    main()
