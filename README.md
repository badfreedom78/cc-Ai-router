# AI Router · 轻量级 Claude Code 智能路由

**中文** ｜ [English](README.en.md)

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![tests](https://github.com/badfreedom78/cc-Ai-router/actions/workflows/tests.yml/badge.svg)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)

把 **Claude Code** 接到多个大模型 API 之上，按任务难度**自动选择合适档位的模型**，
并在某个 API 失效 / 限额时**自动切换**到同档位的备用模型，让工作不中断。

- **轻量**：纯标准库实现的规则分类器，绝大多数请求**不额外调用路由模型**；无后台线程，空闲 CPU = 0。
- **高可用**：多免费 API 站点随时可能失效，本程序会在报错时自动换模型、自动升级档位重试。
- **可视化**：自带一个本地控制台（GUI），可增删模型、查看实时日志、查看今日用量、一键更新依赖。
- **中英双语界面**：控制台可在「设置 → 界面语言」一键切换中文 / English。
- **本地运行**：所有密钥与问答内容都只在本机流转，不上传任何第三方服务器。

---

## 一、它是怎么工作的

```
Claude Code
    │  ANTHROPIC_BASE_URL = http://127.0.0.1:4001
    ▼
Router Proxy  (:4001)        ← 本项目的核心
    │  1. 判断任务难度（规则优先，拿不准才调 Router 模型）
    │  2. 映射到 easy / balanced / strong 三档
    │  3. 在同档候选里按能力（视觉/音频）与优先级挑模型
    │  4. 失败自动换同档 / 相邻档候选，逐个重试
    ▼
LiteLLM 网关  (:4000)        ← 由本项目自动拉起
    │
    ▼
你的多个模型 API（Anthropic 兼容接口）
```

三档语义：

| 档位 | 适用任务 | 典型模型 |
|---|---|---|
| `easy` | 简单改写、翻译、单函数 | 小而快的模型 |
| `balanced` | 日常主力：改一个模块、分析普通 Bug | 中等能力模型 |
| `strong` | 多文件重构、高风险系统、复杂定位 | 最强模型 |

> 路由模型使用**英文提示词**（`router_prompt.md`），以获得更广的模型兼容性；
> 想省 token 可直接编辑该文件裁剪内容，不用改代码。

---

## 二、快速开始（3 步）

### 1. 准备 Python

安装 **Python 3.10 或更高版本**：<https://www.python.org/downloads/>
安装时**务必勾选 “Add Python to PATH”**。

### 2. 双击 `第一次运行.bat`

它会自动完成：

1. 找到可用的 Python；
2. 在 `gui\.venv` 创建一个独立虚拟环境（不会污染系统 Python）；
3. 联网安装 `litellm`、`fastapi` 等依赖（**首次较慢，请耐心等待**）；
4. 由 `config.example.yaml` 生成 `config.yaml`；
5. 打开可视化控制台。

> 依赖安装失败时，脚本会给出使用国内镜像的命令，可复制执行。

### 3. 填写自己的 API

用记事本打开 **`config.yaml`**，把每个模型下的
`api_base` 与 `api_key` 换成**你自己的真实值**，保存。
（详见下一节。）

填好后，在控制台里点 **LiteLLM → 启动**、**Router Proxy → 启动**，即可接入 Claude Code。

> 日常启动请双击 **`启动 AI Router.vbs`** —— 它不会残留任何命令行窗口。

---

## 三、配置 `config.yaml`

配置文件由模板 `config.example.yaml` 复制而来，**只含占位符**。
每个模型是一条 `model_list` 条目：

```yaml
- model_name: balanced                       # 档位名，程序按前缀识别，别乱改
  litellm_params:
    model: anthropic/your-balanced-model     # 上游真实模型名，格式 provider/model
    api_base: https://your-api-endpoint.example.com/anthropic/v1/messages   # ← 换成你的接口地址
    api_key: sk-your-key-here                # ← 换成你的密钥
  model_info:
    supports_vision: true                    # 是否支持看图
    supports_audio_input: false              # 是否支持音频输入
```

**几点约定（重要）**

- **`model_name` 决定档位**，程序按前缀识别：
  `router` / `router-back` / `easy` / `balanced` / `strong`。
  可以加 `easy-com`、`balanced-mid` 这类同档补充候选，但**不要改动前缀**。
- **`api_base` 必须以 `/v1/messages` 结尾**（Anthropic Messages 兼容接口）。
  不同服务商的路径可能不同，请按服务商文档填写，例如：
  - 聚合站：`https://你的站点/模型前缀/anthropic/v1/messages`
  - MiniMax 官方：`https://api.minimax.chat/anthropic/v1/messages`
- **本文件请保持纯 ASCII**，不要写中文注释 ——
  LiteLLM 在中文 Windows（GBK 默认编码）下解析含中文的 YAML 会崩溃。
- **`config.yaml` 已被 `.gitignore` 忽略**，请把真实密钥只写在这里，不要写进 `config.example.yaml`。
- 想让某个档位用更强的模型，直接改 `model`；想调整同档优先级，
  调整 `model_list` 里的**先后顺序**即可（越靠前越优先）。

`router` / `router-back` 这两个是**路由模型**（负责给任务打分），
其余 `easy` / `balanced` / `strong` 才是真正干活的模型。
如果你的接口不支持工具调用或推理模型，`router` 随便选一个便宜、快的即可。

---

## 四、接入 Claude Code

在**启动 Claude Code 的终端**里设置以下环境变量（仅当前会话生效）：

```bat
set ANTHROPIC_BASE_URL=http://127.0.0.1:4001
set ANTHROPIC_AUTH_TOKEN=sk-anything
set ANTHROPIC_MODEL=router
```

Windows PowerShell：

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:4001"
$env:ANTHROPIC_AUTH_TOKEN = "sk-anything"
$env:ANTHROPIC_MODEL = "router"
```

`ANTHROPIC_AUTH_TOKEN` 填什么都可以（代理不校验），保持 `sk-anything` 即可。

**想永久生效**：把下面片段写入 `%USERPROFILE%\.claude\settings.json`
（用户级）或项目内 `.claude\settings.json`：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4001",
    "ANTHROPIC_AUTH_TOKEN": "sk-anything",
    "ANTHROPIC_MODEL": "router"
  }
}
```

控制台的 **「Claude Code」** 标签页里有同样的内容，可一键复制。

---

## 五、可视化控制台

启动后是一个本地网页界面（默认端口 **5178**），包含六个标签页：

- **模型配置** —— 增删改模型；顶部有**路由模式横幅**，每张卡片可一键
  **「强制选择」**（固定走某个模型，临时关闭智能路由）/
  **「取消强制」**（恢复智能路由）。改完即时生效，无需重启。
  页面底部是**用量面板**：最近 10 条请求（模型 / 难度档位 / token），
  以及今日各模型的 token 用量与请求数条状图。
- **LiteLLM / Router Proxy** —— 启动、停止两个服务，查看实时日志与健康状态。
- **Claude Code** —— 环境变量与 `settings.json` 片段。
- **设置** —— **界面语言切换（中文 / English）**、外部依赖版本查看、
  **一键更新依赖**（带实时日志），以及「关于我们」。

界面语言选择会保存到 `gui_settings.json`，重开控制台后依然生效。
切换语言**只影响界面文案**，不影响路由逻辑与后端日志。

---

## 六、目录结构

```
ai-router-release/
├─ 第一次运行.bat          ← 首次安装并启动（双击这个）
├─ 启动 AI Router.vbs      ← 日常启动（无命令行窗口）
├─ config.example.yaml     ← 配置模板（占位符，可安全提交）
├─ config.yaml             ← 你的真实配置（首次运行后生成，已 gitignore）
├─ README.md / README.en.md
├─ LICENSE                 ← MIT 许可证
├─ .gitignore
│
├─ router.py               ← 路由逻辑：难度打分、档位与模型选择
├─ classifier.py           ← 纯标准库规则分类器（省掉大部分路由 LLM 调用）
├─ failover.py             ← 模型健康冷却与失败切换
├─ model_priority.py       ← 按 config.yaml 顺序推导同档优先级
├─ proxy.py                ← Router Proxy 服务（:4001）
├─ router_prompt.md        ← 路由模型的提示词（英文，可自行裁剪以省 token）
│
├─ gui/
│   ├─ main.py              ← 控制台入口（桌面窗口 / 浏览器）
│   ├─ app.py               ← 控制台本地 HTTP 接口
│   ├─ backend.py           ← 进程编排、配置读写、用量聚合、语言设置
│   ├─ requirements.txt     ← 依赖清单
│   └─ frontend/index.html  ← 控制台界面（含中英双语字典）
│
└─ tests/
    └─ test_offline.py     ← 离线自测（45 项，不联网、不依赖 LiteLLM）
```

> 仓库另含 `.github/workflows/tests.yml`：每次 push / PR 会在
> Ubuntu 与 Windows × Python 3.10 / 3.12 上自动跑一遍上面的离线自测。

**运行时自动生成**（无需手动创建）：

| 路径 | 用途 |
|---|---|
| `logs/qa_log.jsonl` | 问答日志（用量面板的数据来源） |
| `router_override.json` | 「强制选择」的状态文件 |
| `gui_settings.json` | 界面语言设置 |
| `gui/gui_error.log` | 控制台崩溃日志（窗口模式下无控制台时的兜底） |
| `gui/.venv/` | 首次运行时创建的虚拟环境 |

---

## 七、常见问题

**Q：界面显示英文，怎么切回中文？**
「设置 → 界面语言 → 中文」。两种语言内容完全等价。

**Q：启动后 `Router Proxy` 起不来，提示找不到 Python 解释器？**
先确认第 2 步的依赖安装已完成（`gui\.venv` 目录存在且非空）。
仍失败时，重新双击 `第一次运行.bat` 让它重装依赖。

**Q：模型报错 / 限额了怎么办？**
不用管。程序会自动在**同档位**换一个模型重试；
若同档全部不可用，会尝试相邻档位。响应头 `x-router-model`
可看到本次实际使用的模型；控制台的用量面板也能看到。

**Q：想完全关闭智能路由，固定用某个模型？**
在「模型配置」页点击该模型的 **「强制选择」** 即可，改完立即生效。

**Q：想临时关掉「规则优先」（每个请求都走路由模型）？**
设置环境变量 `ROUTER_RULE_FIRST=0` 后重启 Router Proxy 即可；
设回 `1`（默认）恢复。

**Q：改了 `config.yaml` 要重启吗？**
要。`config.yaml` 由 LiteLLM 读取，改完请在控制台点 LiteLLM 的**停止 → 启动**。

**Q：控制台窗口一闪而过 / 打不开？**
查看 `gui\gui_error.log`。或在终端里运行
`gui\.venv\Scripts\python.exe gui\main.py` 直接看报错。

---

## 八、端口一览

| 端口 | 服务 | 说明 |
|---|---|---|
| `5178` | 可视化控制台 | 仅本机访问 |
| `4000` | LiteLLM 网关 | 由控制台自动拉起 |
| `4001` | Router Proxy | **Claude Code 指向这里** |

---

## 九、关于

- 作者：**badfreedom78/lzl**
- 最后更新：2026-10-10
- 全部密钥与问答内容仅在本机流转，不会上传到任何第三方服务器。
