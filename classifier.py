"""确定性规则优先分类器（第一级分类）。

这是「轻量化」路线的核心：对一眼就能判断的任务，直接用关键词规则给出
与 Router LLM 同构的 score，从而**完全跳过那一次 LLM 调用**——省掉一次
网络往返和一次完整的 prompt prefill，这是弱机器上最划算的优化。

只有以下情况才交回 Router LLM 兜底（第二级）：

- 信号冲突 / 贴近档位边界（置信度不足）；
- 极短 follow-up（"继续" / "接着"），可能继承很长的上下文，不敢下结论；
- 完全没有命中任何关键词（看不懂在说什么）。

设计上刻意保持「零依赖 + 常量级内存」：只有若干关键词正则表，
模块导入时编译一次，之后常驻内存可忽略。
不使用任何本地模型 / 向量 / 语义索引（那会吃内存，与本项目目标冲突）。

打分口径与 router.select_tier 对齐：

    strong   : risk >= 8 或 complexity >= 8 或 reasoning >= 8
    balanced : complexity >= 5 或 reasoning >= 5
    easy     : 其余

借鉴要点（见 SIMILAR_PROJECTS.md 第七节）：
- 每个维度取「命中的最大权重」而不是求和 —— 防止关键词堆砌刷分；
- 多部分任务按句切分，各段独立打分后取最难的一段；
- 极短 follow-up 直接放弃判断，交给 LLM。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 只对文本前 N 个字符做规则匹配。
# 用户可能粘贴几万行日志，全量正则匹配会拖慢单次请求；
# 分类只看开头已经足够。
MAX_CLASSIFY_CHARS = 4000

# follow-up 判定时允许的最大长度（按字符）
FOLLOWUP_MAX_CHARS = 14


# ============================================================
# 关键词银行
#
# 每个条目 = (编译好的正则, 权重)
# 权重语义：命中后，对应维度「至少」提到这个分值。
# 同一维度多个命中时取最大值（不是求和）。
# ============================================================


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


# --- 复杂度：高（7-9 分）------------------------------------

COMPLEXITY_HARD = [
    (_rx(r"架构|系统设计|整体设计|顶层设计|技术选型"), 8),
    (_rx(r"分布式|集群|微服务|服务治理|高可用|容灾|限流|熔断"), 9),
    (_rx(r"并发|并行|多线程|多进程|协程|线程安全|锁竞争|race\s*condition|deadlock|死锁|竞态"), 9),
    (_rx(r"迁移|重构|重写|改造|evolution|migration|migrate"), 8),
    (_rx(r"内存泄漏|memory\s*leak|性能瓶颈|profiling|压测"), 9),
    (_rx(r"性能优化|性能调优|吞吐|延迟优化"), 8),
    (_rx(r"编译器|解释器|parser|语法树|ast|虚拟机|字节码"), 9),
    (_rx(r"事件溯源|event\s*sourcing|cqrs|状态机|工作流引擎|调度器"), 9),
    (_rx(r"跨模块|跨服务|全链路|端到端|整个项目|整个仓库|整个系统|大规模|large[- ]scale"), 8),
    (_rx(r"安全审计|security\s*audit|渗透|漏洞|exploit|越权|注入"), 9),
    (_rx(r"一致性协议|raft|paxos|分库分表|sharding|一致性哈希"), 9),
]

# --- 复杂度：中（5-6 分）------------------------------------

COMPLEXITY_MEDIUM = [
    (_rx(r"实现|开发|编写|新增|添加|增加|接入|对接|集成|implement|develop|integrate"), 5),
    (_rx(r"功能|模块|接口|api|服务|组件|feature|module|service"), 5),
    (_rx(r"修复|修正|解决|排查|定位|调试|debug|fix|troubleshoot"), 5),
    (_rx(r"优化|改进|调整|重构小块|optimi|improve|enhance"), 5),
    (_rx(r"测试|单元测试|集成测试|回归|test|unit\s*test"), 5),
    (_rx(r"部署|上线|发布|配置|脚本|批量|自动化|deploy|script"), 5),
]

# --- 复杂度：低（2 分）--------------------------------------

COMPLEXITY_EASY = [
    (_rx(r"错别字|拼写|typo|笔误"), 2),
    (_rx(r"改个名|改名|重命名|rename"), 2),
    (_rx(r"格式化|排版|缩进|换行|对齐|format|lint|prettify"), 2),
    (_rx(r"加注释|补注释|注释|comment"), 2),
    (_rx(r"翻译一下|翻译成|译成|translate"), 2),
    (_rx(r"写个函数|写一个函数|写个小|简单|trivial|one[- ]liner|小改|微调"), 2),
    (_rx(r"打印|输出一句话|hello\s*world|示例代码|demo"), 2),
    (_rx(r"写一篇|撰写|写一段|起个标题|取个名字|拟一个|写个文案"), 2),
]

# --- 推理：高（7-9 分）--------------------------------------

REASONING_HARD = [
    (_rx(r"证明|推导|形式化|定理|公式|数学|求导|积分"), 9),
    (_rx(r"算法|复杂度|时间复杂度|最优解|启发式|动态规划|图算法"), 8),
    (_rx(r"根因|root\s*cause|因果|为什么会出现|深入分析|推演"), 8),
    (_rx(r"加密|解密|cryptograph|签名|哈希算法|密钥交换"), 8),
    (_rx(r"权衡|trade[- ]?off|方案对比|架构决策|技术选型"), 7),
]

# --- 推理：中（5 分）----------------------------------------

REASONING_MEDIUM = [
    (_rx(r"分析|比较|对比|评估|判断|reason|analy|evaluat|assess|compar"), 5),
    (_rx(r"设计|方案|策略|思路|规划|design|strateg|plan"), 5),
]

# --- 推理：低（2 分）----------------------------------------

REASONING_EASY = [
    (_rx(r"翻译|格式化|改名|重命名|typo|错别字|拼写|直接"), 2),
    (_rx(r"写一篇|撰写|写一段|写个文案|写封邮件|起个标题|取个名字|润色"), 2),
]

# --- 风险（6-8 分；基线 3）----------------------------------

RISK = [
    (_rx(r"支付|资金|金额|转账|结算|对账|交易|金融|账单|计费|billing|payment"), 8),
    (_rx(r"生产环境|线上|正式服|prod\b|production|灰度"), 7),
    (_rx(r"数据库一致性|数据丢失|不可逆|删除数据|清空数据|drop\s+table|truncate"), 8),
    (_rx(r"权限|鉴权|认证|越权|授权|auth|permission|rbac"), 7),
    (_rx(r"密钥|密码|凭证|secret|credential|token|私钥"), 7),
    (_rx(r"安全|漏洞|泄露|合规|审计|加密"), 7),
    (_rx(r"备份|回滚|容灾|数据迁移"), 7),
]

# --- 工具需求（4-6 分；基线 2）------------------------------

TOOL = [
    (_rx(r"读文件|打开文件|查看文件|读取|cat\b|read\s+file"), 5),
    (_rx(r"搜索|查找|检索|grep|find|search|ripgrep"), 5),
    (_rx(r"运行|执行|跑一下|跑测试|跑一下测试|run\b|execute"), 6),
    (_rx(r"安装|部署|编译|构建|打包|install|deploy|build|make\b"), 6),
    (_rx(r"git|提交|commit|push|pull|merge|rebase|分支"), 5),
    (_rx(r"创建文件|写文件|删除文件|批量|脚本"), 5),
]

# --- 上下文需求（7 分）--------------------------------------

CONTEXT = [
    (_rx(r"整个项目|整个仓库|全部代码|所有文件|全部文件|entire\s+repo|whole\s+codebase"), 7),
    (_rx(r"跨文件|多个文件|整个模块|跨模块|across\s+files|all\s+files"), 7),
    (_rx(r"历史对话|之前的对话|前面的对话|上文|之前提到|刚刚说的|previous\s+conversation"), 7),
    (_rx(r"全文|通读|整体阅读|review\s+the\s+whole"), 7),
]

# --- 平凡输入（直接判 easy）--------------------------------

TRIVIAL_RE = _rx(
    r"^(hi|hello|hey|yo|你好|您好|嗨|在吗|在不在|谢谢|多谢|感谢|thanks|thank\s+you|thx|"
    r"ok|okay|好的|收到|明白|test|测试一下|ping|1|\.)$"
)

# --- 极短 follow-up（放弃判断，交给 LLM）--------------------

FOLLOWUP_RE = _rx(
    r"^(继续|接着|然后呢|然后|再来|继续吧|下一步|还有呢|接着做|"
    r"next|go\s+on|continue|keep\s+going|then\??|more|yes|no|"
    r"go\s+ahead|do\s+it|嗯+|好+|可以|行|中)$"
)

# --- 任务类型判定 -------------------------------------------

TASK_TYPE_BANKS = {
    "coding": [
        _rx(r"代码|函数|方法|类|变量|循环|数组|报错|异常|栈|堆|"
            r"bug|debug|编译|构建|接口|api|数据库|sql|索引|"
            r"python|javascript|typescript|java\b|golang|rust|c\+\+|shell|脚本|"
            r"依赖|包管理|单元测试|重构|git|部署|docker|容器"),
    ],
    "research": [
        _rx(r"调研|研究|查阅|搜索资料|查一下|有哪些|资料|论文|文献|"
            r"对比|评测|benchmark|survey|综述|现状"),
    ],
    "writing": [
        _rx(r"写一篇|文案|文章|稿|报告|总结|摘要|润色|宣传|邮件|"
            r"blog|博客|新闻稿|讲话|致辞"),
    ],
    "analysis": [
        _rx(r"分析数据|统计数据|数据|指标|图表|报表|趋势|可视化|"
            r"metrics|dashboard|回归|相关性|统计"),
    ],
}

TASK_TYPE_ORDER = ["coding", "analysis", "research", "writing"]


# ============================================================
# 多部分任务切分
# ============================================================

_SPLIT_RE = _rx(
    r"[；;。\n]|，?然后|并且|以及|同时|另外|接着|，?再|"
    r",\s*then\b|\band\s+then\b|\balso\b|;\s*"
)

MAX_PARTS = 4


@dataclass
class RuleResult:
    """规则分类结果。"""

    score: dict
    confidence: float
    reason: str
    tier: str
    matched: list[str] = field(default_factory=list)


# ============================================================
# 内部工具
# ============================================================

def _max_weight(bank: list[tuple[re.Pattern, int]], text: str) -> tuple[int, str]:
    """返回 (命中最大权重, 命中的模式)。未命中返回 (0, "")。"""

    best = 0
    best_pat = ""

    for pattern, weight in bank:
        if weight > best and pattern.search(text):
            best = weight
            best_pat = pattern.pattern

    return best, best_pat


def _split_parts(text: str) -> list[str]:
    parts = [p.strip() for p in _SPLIT_RE.split(text) if p and p.strip()]

    # 切得太碎反而失真；保留最长的若干片段
    if len(parts) <= 1:
        return [text]

    parts.sort(key=len, reverse=True)
    return parts[:MAX_PARTS]


def _detect_task_type(text: str) -> str:
    hits = {
        name: sum(1 for p in bank if p.search(text))
        for name, bank in TASK_TYPE_BANKS.items()
    }

    best_name = "other"
    best_hits = 0

    for name in TASK_TYPE_ORDER:
        n = hits.get(name, 0)
        if n > best_hits:
            best_hits = n
            best_name = name

    return best_name if best_hits > 0 else "other"


def _clamp(v: int, lo: int = 1, hi: int = 10) -> int:
    return max(lo, min(hi, v))


# ============================================================
# 主入口
# ============================================================

def classify(
    text: str,
    has_history: bool = False,
    history_turns: int = 0,
) -> RuleResult:
    """对一段用户文本做确定性分类。

    Args:
        text: 当前这条用户输入（不含历史）。
        has_history: 上游是否带了多轮历史。
        history_turns: 历史消息条数（用于估算上下文需求）。

    Returns:
        RuleResult，其中 ``score`` 与 Router LLM 的输出同构，
        ``confidence`` 为 0~1，越高表示越有把握（>= 阈值即可跳过 LLM）。
    """

    raw = (text or "").strip()
    snippet = raw[:MAX_CLASSIFY_CHARS].lower()

    if not raw:
        return RuleResult(
            score=_easy_score(),
            confidence=1.0,
            reason="空输入",
            tier="easy",
        )

    # --- 平凡输入：直接 easy ---------------------------------

    if TRIVIAL_RE.match(raw):
        return RuleResult(
            score=_easy_score(),
            confidence=1.0,
            reason="平凡输入",
            tier="easy",
        )

    # --- 极短 follow-up：放弃判断，交回 LLM ------------------

    if len(raw) <= FOLLOWUP_MAX_CHARS and FOLLOWUP_RE.match(raw):
        return RuleResult(
            score=_mid_score(),
            confidence=0.0,
            reason="极短 follow-up，需 LLM 结合上下文判断",
            tier="balanced",
        )

    # --- 多部分任务：取最难的一段 ---------------------------

    parts = _split_parts(snippet)
    multi = len(parts) > 1

    c_hard = c_med = c_easy = 0
    r_hard = r_med = r_easy = 0
    risk_w = 0
    tool_w = 0
    ctx_w = 0
    matched: list[str] = []
    # 命中的「信号类别」集合：复杂度 / 推理 / 风险 / 工具 / 上下文。
    # 用类别数（而不是命中次数）衡量把握程度，避免关键词堆砌刷分。
    signals: set[str] = set()

    def _note(category: str, weight: int, pattern: str) -> None:
        if weight > 0:
            signals.add(category)
            if pattern and len(matched) < 8:
                matched.append(pattern)

    for part in parts:
        v, p = _max_weight(COMPLEXITY_HARD, part)
        c_hard = max(c_hard, v)
        _note("complexity", v, p)

        v, p = _max_weight(COMPLEXITY_MEDIUM, part)
        c_med = max(c_med, v)
        _note("complexity", v, p)

        v, p = _max_weight(COMPLEXITY_EASY, part)
        c_easy = max(c_easy, v)
        _note("complexity", v, p)

        v, p = _max_weight(REASONING_HARD, part)
        r_hard = max(r_hard, v)
        _note("reasoning", v, p)

        v, p = _max_weight(REASONING_MEDIUM, part)
        r_med = max(r_med, v)
        _note("reasoning", v, p)

        v, p = _max_weight(REASONING_EASY, part)
        r_easy = max(r_easy, v)
        _note("reasoning", v, p)

        v, p = _max_weight(RISK, part)
        risk_w = max(risk_w, v)
        _note("risk", v, p)

        v, p = _max_weight(TOOL, part)
        tool_w = max(tool_w, v)
        _note("tool", v, p)

        v, p = _max_weight(CONTEXT, part)
        ctx_w = max(ctx_w, v)
        _note("context", v, p)

    # --- 各维度定分 -----------------------------------------

    if c_hard:
        complexity = max(7, c_hard)
    elif c_med:
        complexity = max(5, c_med)
    elif c_easy:
        complexity = 2
    else:
        complexity = 3

    if r_hard:
        reasoning = max(7, r_hard)
    elif r_med:
        reasoning = max(5, r_med)
    elif r_easy:
        reasoning = 2
    else:
        reasoning = 3

    if risk_w:
        risk = max(6, risk_w)
    elif c_easy:
        risk = 2
    else:
        risk = 3

    # 多部分任务整体更重
    if multi:
        complexity = _clamp(complexity + 1)
        reasoning = _clamp(reasoning + 1)

    # 短 follow-up 但带了历史：很可能要继承长上下文
    if has_history and len(raw) < 24:
        complexity = _clamp(complexity + 1)

    context_requirement = 3
    if ctx_w:
        context_requirement = max(7, ctx_w)
    if multi:
        context_requirement = _clamp(context_requirement + 1)
    if history_turns >= 6:
        context_requirement = _clamp(context_requirement + 1)

    task_type = _detect_task_type(snippet)

    tool_requirement = 2
    if tool_w:
        tool_requirement = max(4, tool_w)
    if task_type == "coding":
        tool_requirement = max(4, tool_requirement)

    complexity = _clamp(complexity)
    reasoning = _clamp(reasoning)
    risk = _clamp(risk)

    # --- tier 与上下文行动 ----------------------------------

    if risk >= 8 or complexity >= 8 or reasoning >= 8:
        tier = "strong"
    elif complexity >= 5 or reasoning >= 5:
        tier = "balanced"
    else:
        tier = "easy"

    partial = context_requirement >= 7

    score = {
        "task_type": task_type,
        "complexity": complexity,
        "reasoning": reasoning,
        "risk": risk,
        "context_requirement": context_requirement,
        "tool_requirement": tool_requirement,
        "context_sufficiency": "partial" if partial else "sufficient",
        "missing_context": [],
        "need_retrieval": partial,
        "need_user_clarification": False,
        "requires_vision": bool(
            re.search(r"图片|截图|图里|图中|这张图|照片|screenshot|image", snippet)
        ),
        "requires_audio": bool(
            re.search(r"音频|录音|语音|audio|voice", snippet)
        ),
    }

    hits = len(signals)
    confidence = _confidence(
        complexity=complexity,
        reasoning=reasoning,
        risk=risk,
        hits=hits,
    )

    reason = (
        f"tier={tier} complexity={complexity} reasoning={reasoning} "
        f"risk={risk} type={task_type} signals={hits}"
    )

    return RuleResult(
        score=score,
        confidence=confidence,
        reason=reason,
        tier=tier,
        matched=sorted(set(matched)),
    )


def _confidence(
    *,
    complexity: int,
    reasoning: int,
    risk: int,
    hits: int,
) -> float:
    """估算规则分类的置信度（0~1）。

    核心是「离决策边界有多远」：tier 的分界在 5 和 8。
    信号贴着边界时，LLM 很容易给出相邻档位 -> 不敢下结论。
    """

    m = max(complexity, reasoning)

    dists = [abs(m - 5), abs(m - 8)]

    # 只有当「复杂度/推理」还没到 strong 线（m < 8）时，
    # risk 才是可能翻档的信号，此时才把它的边界距离计入。
    # 一旦 m >= 8，tier 已经由复杂度/推理定死，risk 是否贴边界不再重要。
    if risk >= 5 and m < 8:
        dists.append(abs(risk - 8))

    margin = min(dists + [3.0])

    conf = margin / 3.0

    # 命中越多越可信；一个都没命中说明没看懂
    if hits == 0:
        conf *= 0.4
    elif hits >= 2:
        conf = min(1.0, conf + 0.3)

    # 信号极强或极弱，额外加分
    if m >= 9 or m <= 2:
        conf = min(1.0, conf + 0.2)

    return round(conf, 3)


# ============================================================
# 兜底 score（供内部使用）
# ============================================================

def _easy_score() -> dict:
    return {
        "task_type": "other",
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


def _mid_score() -> dict:
    return {
        "task_type": "other",
        "complexity": 5,
        "reasoning": 5,
        "risk": 5,
        "context_requirement": 5,
        "tool_requirement": 4,
        "context_sufficiency": "partial",
        "missing_context": [],
        "need_retrieval": False,
        "need_user_clarification": False,
        "requires_vision": False,
        "requires_audio": False,
    }


# ============================================================
# 自测入口（python classifier.py）
# ============================================================

if __name__ == "__main__":

    samples = [
        "把 README 里的错别字改一下",
        "帮我重命名这个变量",
        "你好",
        "继续",
        "帮我实现一个用户登录功能",
        "帮我重构整个项目的鉴权模块，并加上并发保护",
        "设计一个支持水平扩展的分布式订单系统，要处理支付一致性和幂等",
        "写一篇关于 AI 路由的博客",
        "分析一下这份销售数据，出个趋势图",
        "为什么这个函数会内存泄漏？帮我定位根因",
        "asdfghjkl",
    ]

    for s in samples:
        r = classify(s)
        flag = "跳过LLM" if r.confidence >= 0.7 else "走LLM"
        print(f"[{flag}] conf={r.confidence:.2f} tier={r.tier:<8} | {s}")
        print(f"        {r.reason}")
