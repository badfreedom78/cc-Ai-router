"""模型优先级：以 config.yaml 的 model_list 顺序为准。

设计动机（GUI 拖放排序 -> 调用优先级）：

用户在 GUI 里把模型按层（router / easy / balanced / strong）
分组展示，并可通过拖放调整同层内的先后顺序。这个顺序
「就是」调用优先级：同档位失败切换时按此顺序逐个尝试。

为了让纯标准库的 router.py（不依赖 PyYAML）也能读到这个顺序，
这里不解析 YAML，而是用极简的行扫描提取 `model_name:` 字段
的出现顺序 —— config.yaml 由 backend.save_config 生成，
格式固定（"- model_name: xxx"），行扫描足够可靠；
即便手工编辑过，最坏情况是顺序回退到旧 MODEL_PRIORITY，
不会崩溃。

内存与开销：

- 结果按 (mtime, size) 缓存，文件没变时零解析；
- 解析本身只逐行找前缀，O(行数)，config.yaml 很小；
- 无新增第三方依赖。
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"

# 已知的档位前缀（与 router.TIER_ORDER 对齐 + router 专用组）
TIER_PREFIXES = ("router", "easy", "balanced", "strong")

# 缓存：(mtime_ns, size) -> {model_name: priority}
_cache: dict = {"key": None, "value": None}


def _scan_model_names(text: str) -> list[str]:
    """
    从 config.yaml 文本里按出现顺序提取 model_name 列表。

    匹配两种形态：
      - model_name: xxx            （列表项，save_config 生成）
      - model_name: xxx  # 注释    （容忍行尾注释）
    """

    names: list[str] = []

    for raw_line in text.splitlines():

        line = raw_line.strip()

        if not line.startswith("- "):
            continue

        body = line[2:].strip()

        if not body.startswith("model_name:"):
            continue

        value = body[len("model_name:"):].strip()

        # 去掉行尾注释与引号
        for sep in (" #", "\t#"):
            idx = value.find(sep)
            if idx != -1:
                value = value[:idx].strip()

        value = value.strip("\"'")

        if value:
            names.append(value)

    return names


def load_model_priority(path: Path | None = None) -> dict[str, int]:
    """
    返回 {model_name: priority}。

    priority 从 1 开始，按 model_name 在 config.yaml 中出现的
    先后顺序递增（同层内先出现的优先级更高）。

    文件缺失 / 解析不到任何名字时返回空 dict，
    调用方（router.find_capable_models）应回退到内置 MODEL_PRIORITY。
    """

    cfg = path or CONFIG_PATH

    try:
        stat = cfg.stat()
        key = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        _cache["key"] = None
        _cache["value"] = None
        return {}

    if _cache["key"] == key and _cache["value"] is not None:
        return _cache["value"]

    try:
        text = cfg.read_text(encoding="ascii", errors="replace")
    except OSError:
        return {}

    names = _scan_model_names(text)

    priority: dict[str, int] = {}

    for name in names:
        if name not in priority:
            priority[name] = len(priority) + 1

    _cache["key"] = key
    _cache["value"] = priority

    return priority


def tier_of(model_name: str) -> str | None:
    """根据模型名推导所属层（router / easy / balanced / strong）。"""

    for t in TIER_PREFIXES:
        if model_name == t or model_name.startswith(t + "-"):
            return t

    return None


def invalidate_cache() -> None:
    """config 被重写后清掉缓存（下一次访问会重新解析）。"""

    _cache["key"] = None
    _cache["value"] = None


if __name__ == "__main__":
    # 简单自测
    p = load_model_priority()
    print("config.yaml 模型顺序:")
    for name, pr in sorted(p.items(), key=lambda x: x[1]):
        print(f"  {pr}. {name}  (tier={tier_of(name)})")
