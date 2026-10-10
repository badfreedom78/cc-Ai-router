"""模型健康管理：API 失效 / 限额时的冷却与自动切换支持。

设计目标（与项目「轻量化」约束一致）：

- 纯标准库，零第三方依赖；
- 内存开销 = 每个模型一个 float（冷却截止时间戳），
  常驻内存只有几十字节量级；
- 不做后台探测线程、不做定时器 —— 一切都在请求路径上
  按需判断，空闲时 CPU 占用为 0。

解决的问题：

免费 API 站点随时可能失效、或 5 小时限额到期（429）。
没有本模块时，上游一报错 proxy 只能把错误透传给
Claude Code，工作被迫中断、需要手动切模型。
有了本模块，proxy 在转发失败时可以：

1. 标记该模型进入「冷却期」（期间不再选它）；
2. 立刻换同档位的其他模型重试，用户无感。

冷却时长按错误类型区分（默认值，可用环境变量覆盖）：

  429 (限额/限流)   -> ROUTER_COOLDOWN_429  默认 900 秒（15 分钟）
                       若上游给了 Retry-After 头则优先用它
  401/403 (key失效) -> ROUTER_COOLDOWN_AUTH 默认 1800 秒（30 分钟）
  5xx (上游故障)    -> ROUTER_COOLDOWN_5XX  默认 120 秒（2 分钟）
  其他 4xx (如 400) -> ROUTER_COOLDOWN_4XX  默认 60 秒（1 分钟）

成功一次即清除该模型的冷却（快速恢复）。
"""

import os
import time

# ------------------------------------------------------------
# 冷却时长（秒），环境变量可覆盖
# ------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


COOLDOWN_429 = _env_int("ROUTER_COOLDOWN_429", 900)
COOLDOWN_AUTH = _env_int("ROUTER_COOLDOWN_AUTH", 1800)
COOLDOWN_5XX = _env_int("ROUTER_COOLDOWN_5XX", 120)
COOLDOWN_4XX = _env_int("ROUTER_COOLDOWN_4XX", 60)

# 冷却时长上限，防止上游给离谱的 Retry-After
MAX_COOLDOWN = _env_int("ROUTER_COOLDOWN_MAX", 3600)

# ------------------------------------------------------------
# 冷却表：{model_name: 截止时间戳}
#
# 纯 dict、每项一个 float，无锁（asyncio 单线程事件循环内访问）。
# 模型数量是个位数，遍历成本可忽略。
# ------------------------------------------------------------

_cooldowns: dict[str, float] = {}

# 最近一次失败的原因（仅用于 /health 展示与日志）
_last_errors: dict[str, str] = {}


def cooldown_for_status(status_code: int) -> int:
    """根据 HTTP 状态码返回默认冷却秒数。"""

    if status_code == 429:
        return COOLDOWN_429

    if status_code in (401, 403):
        return COOLDOWN_AUTH

    if status_code >= 500:
        return COOLDOWN_5XX

    return COOLDOWN_4XX


def mark_failure(
    model: str,
    status_code: int | None = None,
    reason: str = "",
    retry_after: float | None = None,
) -> float:
    """
    标记模型失败，进入冷却期。

    参数：
        model       : 模型名
        status_code : 上游 HTTP 状态码（None 表示连接层错误）
        reason      : 简短原因（只保留前 200 字节）
        retry_after : 上游 Retry-After 头的秒数（优先于默认值）

    返回：冷却截止时间戳。
    """

    if retry_after is not None and retry_after > 0:
        seconds = min(retry_after, MAX_COOLDOWN)
    else:
        seconds = cooldown_for_status(status_code or 0)

    # 连接层错误（LiteLLM 本身不通）：上游整体故障可能性大，
    # 短冷却即可，不要长冷却把所有模型都藏起来。
    if status_code is None:
        seconds = min(seconds, COOLDOWN_4XX)

    until = time.time() + seconds

    _cooldowns[model] = until
    _last_errors[model] = (reason or str(status_code))[:200]

    return until


def mark_success(model: str) -> None:
    """成功一次立即清除冷却（免费 API 恢复快，别浪费）。"""

    _cooldowns.pop(model, None)
    _last_errors.pop(model, None)


def is_cooling(model: str, now: float | None = None) -> bool:
    """模型当前是否在冷却期。"""

    until = _cooldowns.get(model)
    if until is None:
        return False

    t = now if now is not None else time.time()

    if t >= until:
        # 冷却结束，顺手清理
        _cooldowns.pop(model, None)
        return False

    return True


def cooling_status(now: float | None = None) -> dict[str, float]:
    """返回仍在冷却中的模型 -> 剩余秒数（给 /health 与日志用）。"""

    t = now if now is not None else time.time()

    out: dict[str, float] = {}

    for model, until in list(_cooldowns.items()):

        remain = until - t

        if remain <= 0:
            _cooldowns.pop(model, None)
            _last_errors.pop(model, None)
            continue

        out[model] = round(remain, 1)

    return out


def last_error(model: str) -> str:
    return _last_errors.get(model, "")


def reset() -> None:
    """清空全部冷却状态（测试 / 手动恢复用）。"""

    _cooldowns.clear()
    _last_errors.clear()
