"""离线自测：规则分类器 / 两级路由 / 代理决策。

不依赖 LiteLLM、不触网，直接用假客户端替换掉上游。
运行：python tests/test_offline.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import classifier  # noqa: E402
import router  # noqa: E402
import failover  # noqa: E402


class _ConnFailClient:
    """模拟连接层失败（LiteLLM 没启动）。"""

    def build_request(self, method, url, json=None, headers=None):
        return object()

    async def send(self, req, stream=False):
        import httpx

        raise httpx.ConnectError("connection refused")

    async def aclose(self):
        pass


def _last_qa_attempts() -> int | None:
    """读取测试 qa_log 的最后一行的 attempts。"""
    try:
        with open(proxy.QA_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
        if lines:
            return json.loads(lines[-1]).get("attempts")
    except Exception:
        pass
    return None


PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# ============================================================
# 1. 规则分类器
# ============================================================

print("\n== classifier ==")

cases = [
    # (文本, 期望 tier, 是否应跳过 LLM)
    ("把 README 里的错别字改一下", "easy", True),
    ("帮我重命名这个变量", "easy", True),
    ("你好", "easy", True),
    ("继续", "balanced", False),
    ("帮我实现一个用户登录功能", "balanced", False),
    ("帮我重构整个项目的鉴权模块，并加上并发保护", "strong", True),
    ("为什么这个函数会内存泄漏？帮我定位根因", "strong", True),
    ("分析一下这份销售数据，出个趋势图", "balanced", False),
    ("asdfghjkl", "easy", False),
]

for text, want_tier, want_skip in cases:
    r = classifier.classify(text)
    skip = r.confidence >= router.RULE_CONFIDENCE_THRESHOLD
    check(
        f"{text[:16]!r} -> tier={r.tier} conf={r.confidence}",
        r.tier == want_tier and skip == want_skip,
        f"(want tier={want_tier} skip={want_skip})",
    )

# score 结构完整性
r = classifier.classify("帮我写一个排序函数")
for key in (
    "task_type", "complexity", "reasoning", "risk",
    "context_requirement", "tool_requirement",
    "context_sufficiency", "missing_context",
    "need_retrieval", "need_user_clarification",
    "requires_vision", "requires_audio",
):
    check(f"score 含字段 {key}", key in r.score)


# ============================================================
# 2. 两级路由 route_score
# ============================================================

print("\n== route_score ==")

score, source = router.route_score(
    "把错别字改一下",
    [{"role": "user", "content": "把错别字改一下"}],
)
check("规则命中走 rule", source == "rule", f"got {source}")

# Router 备用模型切换：第一个 HTTP 失败自动换 router-back
_orig_post = router._post_router_payload
_calls = {"n": 0}


def _flaky_post(payload):
    _calls["n"] += 1
    if _calls["n"] == 1:
        raise router._RouterHTTPError(429, "rate limited")
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "task_type": "coding",
                            "complexity": 2,
                            "reasoning": 2,
                            "risk": 2,
                            "context_requirement": 2,
                            "tool_requirement": 2,
                            "context_sufficiency": "sufficient",
                            "missing_context": [],
                            "need_retrieval": False,
                            "need_user_clarification": False,
                            "requires_vision": False,
                            "requires_audio": False,
                        }
                    )
                },
                "finish_reason": "stop",
            }
        ]
    }


router._post_router_payload = _flaky_post
try:
    s3 = router.call_router([{"role": "user", "content": "测试"}])
    check(
        "Router 429 自动切备用模型",
        _calls["n"] == 2 and "complexity" in s3,
        f"calls={_calls['n']}",
    )
finally:
    router._post_router_payload = _orig_post

# tier 推导
check("easy 任务 -> easy tier", router.select_tier(score) == "easy")

# Router LLM 全挂时降级用低置信度规则结果（而不是抛异常）
_orig_call_router = router.call_router


def _boom(msgs, summary=None):
    raise RuntimeError("all router candidates failed")


router.call_router = _boom
try:
    s2, src2 = router.route_score(
        "帮我实现一个用户登录功能",
        [{"role": "user", "content": "x"}],
    )
    check(
        "LLM 全挂时降级 rule-degraded",
        src2 == "rule-degraded" and router.select_tier(s2) == "balanced",
        f"got {src2}",
    )
finally:
    router.call_router = _orig_call_router


# ============================================================
# 2b. config.yaml 顺序 = 调用优先级（GUI 拖放排序联动）
# ============================================================

print("\n== model_priority ==")

import model_priority  # noqa: E402
import shutil as _shutil

_cfg_bak = os.path.join(ROOT, "config.yaml.bak_test")
_shutil.copy(os.path.join(ROOT, "config.yaml"), _cfg_bak)

try:
    prio = model_priority.load_model_priority()
    check(
        "config 顺序解析出全部模型",
        len(prio) >= 5 and "router" in prio and "strong" in prio,
        f"got {len(prio)} models",
    )
    check(
        "tier_of 前缀推导",
        model_priority.tier_of("balanced-mid") == "balanced"
        and model_priority.tier_of("router-back") == "router"
        and model_priority.tier_of("xyz") is None,
    )
finally:
    if os.path.exists(_cfg_bak):
        _shutil.move(_cfg_bak, os.path.join(ROOT, "config.yaml"))
    model_priority.invalidate_cache()

# 拖放联动（不依赖 GUI backend，直接改写顺序验证 router 候选链）
print("\n== drag reorder -> candidate chain ==")

_cfg = os.path.join(ROOT, "config.yaml")
_orig = open(_cfg, encoding="utf-8").read()
try:
    # 把 easy-com 挪到 easy 前面（纯文本重排，模拟 reorder_models 的效果）
    _shutil.copy(_cfg, _cfg_bak)
    reordered = _orig.replace(
        "- model_name: easy-com",
        "@@EASYCOM@@",
    ).replace(
        "- model_name: easy\n",
        "- model_name: easy-com\n",
        1,
    ).replace(
        "@@EASYCOM@@",
        "- model_name: easy",
    )
    # 上面把 easy 与 easy-com 两行互换（各自的其余字段行不动，
    # 因为 model_priority 只看 model_name 行的顺序）
    with open(_cfg, "w", encoding="utf-8", newline="\n") as f:
        f.write(reordered)

    model_priority.invalidate_cache()

    router.MODEL_CAPS = {
        "easy": {"vision": True, "audio": False},
        "easy-com": {"vision": True, "audio": False},
        "balanced": {"vision": True, "audio": False},
        "strong": {"vision": True, "audio": False},
    }
    chain = router.find_capable_models("easy", False, False)
    check(
        "拖放后候选链第一位变为 easy-com",
        chain[0] == "easy-com",
        f"got {chain}",
    )
finally:
    _shutil.move(_cfg_bak, _cfg)
    model_priority.invalidate_cache()
    router.MODEL_CAPS = {}


# ============================================================
# 3. 代理决策（假上游）
# ============================================================

print("\n== proxy ==")

try:
    from fastapi.testclient import TestClient
    import proxy
except Exception as e:  # pragma: no cover
    print(f"  [skip] 缺少 fastapi/httpx，跳过代理测试：{e}")
    print(f"\n结果：PASS={PASS} FAIL={FAIL}")
    sys.exit(1 if FAIL else 0)


class _FakeResp:
    def __init__(self, payload, status_code=200, headers=None):
        self.status_code = status_code
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = headers or {}

    @property
    def content(self):
        return self._body


class _FakeStreamResp:
    """流式响应：status>=400 时模拟错误。"""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        headers = {}
        if status_code == 429:
            headers["retry-after"] = "600"
        self.headers = headers
        self._body = json.dumps(payload).encode("utf-8")

    async def aread(self):
        return self._body

    async def aclose(self):
        pass


class _FakeClient:
    """替换 proxy.client，可编程的响应序列。"""

    def __init__(self, responses=None):
        # responses: 按请求顺序弹出的 (status, payload) 列表
        self.responses = list(responses or [])
        self.sent_models: list[str] = []

    def build_request(self, method, url, json=None, headers=None):
        self._pending_json = json
        return object()

    async def send(self, req, stream=False):
        self.sent_models.append(self._pending_json["model"])
        status, payload = (
            self.responses.pop(0)
            if self.responses
            else (200, {"ok": True})
        )
        return _FakeStreamResp(status, payload)

    async def post(self, url, json=None, headers=None):
        self.sent_models.append(json["model"])
        status, payload = (
            self.responses.pop(0)
            if self.responses
            else (200, {"ok": True})
        )
        return _FakeResp(payload, status_code=status)

    async def aclose(self):
        pass


def _ok_payload():
    return {
        "id": "msg_1",
        "type": "message",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }


proxy.client = _FakeClient()

# 清掉可能存在的强制路由文件
ov = proxy.OVERRIDE_PATH
saved = None
if os.path.exists(ov):
    with open(ov, "r", encoding="utf-8") as f:
        saved = f.read()
    os.remove(ov)

# 日志写到临时文件，避免污染真实日志
proxy.QA_LOG_PATH = os.path.join(HERE, "_qa_test.jsonl")

tc = TestClient(proxy.app)

# ============================================================
# 3a. 基本转发（无能力表 -> tier 名直传）
# ============================================================

r1 = tc.post(
    "/v1/messages",
    json={
        "model": "router",
        "messages": [{"role": "user", "content": "把这段话的错别字改一下"}],
    },
)
check("代理返回 200", r1.status_code == 200, f"got {r1.status_code}")
check(
    "响应头带 x-router-source=rule",
    r1.headers.get("x-router-source") == "rule",
    f"got {r1.headers.get('x-router-source')}",
)
check(
    "模型被替换为 tier 名（无能力表时）",
    proxy.client.sent_models[-1] == "easy",
    f"got {proxy.client.sent_models[-1]}",
)

# ============================================================
# 3b. 失败自动切换：429 -> 同档位换模型
# ============================================================

print("\n== failover ==")

# 模拟能力表
router.MODEL_CAPS = {
    "easy": {"vision": True, "audio": False},
    "easy-com": {"vision": True, "audio": False},
    "balanced": {"vision": True, "audio": False},
    "balanced-mid": {"vision": True, "audio": False},
    "balanced-weak": {"vision": False, "audio": False},
    "strong": {"vision": True, "audio": False},
}
failover.reset()

# 场景1：第一个模型 429，自动切到第二个
proxy.client = _FakeClient(
    responses=[
        (429, {"type": "error", "error": {"message": "rate limited"}}),
        (200, _ok_payload()),
    ]
)

r3 = tc.post(
    "/v1/messages",
    json={
        "model": "router",
        "messages": [{"role": "user", "content": "把这段话的错别字改一下"}],
    },
)
check(
    "429 后自动切换并成功",
    r3.status_code == 200,
    f"got {r3.status_code}",
)
check(
    "切换顺序 easy -> easy-com",
    proxy.client.sent_models == ["easy", "easy-com"],
    f"got {proxy.client.sent_models}",
)
check(
    "响应头记录尝试次数=2",
    r3.headers.get("x-router-attempts") == "2",
    f"got {r3.headers.get('x-router-attempts')}",
)
check(
    "响应头记录最终模型 easy-com",
    r3.headers.get("x-router-model") == "easy-com",
    f"got {r3.headers.get('x-router-model')}",
)
check(
    "失败模型已进冷却",
    failover.is_cooling("easy"),
    "easy 应在冷却中",
)
check(
    "成功模型不在冷却",
    not failover.is_cooling("easy-com"),
)

# 场景2：冷却中的模型被跳过（easy 还在冷却，应直接选 easy-com）
proxy.client = _FakeClient(responses=[(200, _ok_payload())])

r4 = tc.post(
    "/v1/messages",
    json={
        "model": "router",
        "messages": [{"role": "user", "content": "帮我重命名这个变量"}],
    },
)
check(
    "冷却中的模型被跳过",
    proxy.client.sent_models == ["easy-com"],
    f"got {proxy.client.sent_models}",
)

# 场景3：所有候选都失败 -> 透传最后一个错误
failover.reset()
proxy.client = _FakeClient(
    responses=[
        (429, {"type": "error", "error": {"message": "limit 1"}}),
        (429, {"type": "error", "error": {"message": "limit 2"}}),
        (500, {"type": "error", "error": {"message": "boom"}}),
        (500, {"type": "error", "error": {"message": "boom"}}),
    ]
)

r5 = tc.post(
    "/v1/messages",
    json={
        "model": "router",
        "messages": [{"role": "user", "content": "把这段话的错别字改一下"}],
    },
)
check(
    "全部失败时返回错误",
    r5.status_code == 500,
    f"got {r5.status_code}",
)
check(
    "失败链记录在响应头",
    "easy" in (r5.headers.get("x-router-failed") or ""),
    f"got {r5.headers.get('x-router-failed')}",
)
check(
    "qa_log 记录 attempts=4",
    _last_qa_attempts() == 4,
    f"got {_last_qa_attempts()}",
)

# 场景4：连接层错误不重试，直接 502
failover.reset()
proxy.client = _ConnFailClient()

r6 = tc.post(
    "/v1/messages",
    json={
        "model": "router",
        "messages": [{"role": "user", "content": "把这段话的错别字改一下"}],
    },
)
check(
    "连接失败返回 502 不重试",
    r6.status_code == 502,
    f"got {r6.status_code}",
)

# 场景5：全部候选都在冷却 -> 回退完整列表（不能拒绝服务）
failover.reset()
for m in router.MODEL_CAPS:
    failover.mark_failure(m, 429, "test")
proxy.client = _FakeClient(responses=[(200, _ok_payload())])

r7 = tc.post(
    "/v1/messages",
    json={
        "model": "router",
        "messages": [{"role": "user", "content": "把错别字改一下"}],
    },
)
check(
    "全部冷却时回退完整列表仍能服务",
    r7.status_code == 200
    and proxy.client.sent_models == ["easy"],
    f"got {r7.status_code}, {proxy.client.sent_models}",
)
check(
    "成功后清除该模型的冷却",
    not failover.is_cooling("easy"),
)

# 恢复
router.MODEL_CAPS = {}

# 恢复
if saved is not None:
    with open(ov, "w", encoding="utf-8") as f:
        f.write(saved)

if os.path.exists(proxy.QA_LOG_PATH):
    os.remove(proxy.QA_LOG_PATH)

# 健康检查字段
r2 = tc.get("/health")
check("health 返回 rule_first", "rule_first" in r2.json())


print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
