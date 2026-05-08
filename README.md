![这是图片](./images/title.png)
<div align="center">

<!-- # Grok Search MCP -->

[English](./README.en.md) | [繁體中文](./README.zh-TW.md) | 简体中文 | [日本語](./README.ja.md) | [Русский](./README.ru.md)

**GrokSearch MCP，为多种 MCP 客户端提供轻量、可核验来源的网络上下文能力**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT) [![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/) [![FastMCP](https://img.shields.io/badge/FastMCP-2.3.0+-green.svg)](https://github.com/jlowin/fastmcp)

</div>

---

## 一、概述

GrokSearch MCP 是一个基于 [FastMCP](https://github.com/jlowin/fastmcp) 构建的轻量 MCP 服务器，面向 Claude Code、Codex CLI、Cherry Studio 等支持 MCP 的客户端，提供**最新、可核验、低摩擦**的网络上下文能力。

公开 package import contract 当前分两层：`grok_search.mcp` 是 access-time lazy export，只有真正访问该导出时才需要 `fastmcp`；`grok_search.providers.GrokSearchProvider` 也是 access-time lazy export，普通非 provider 导入不应仅因 Grok provider 相关依赖缺失而被提前拖死。这只是在导入时收口边界，不改变安装时依赖声明，也不应被理解为这些依赖已经变成 optional extras。

它不是一个以浏览器自动化为核心的重型系统，而是一层为研究型问答优化的搜索基础设施：

- `Grok` 负责主答案生成
- `Tavily` 负责搜索控制、网页提取与站点映射
- `Firecrawl` 负责抓取托底与补充信源
- `plan_*` 负责复杂问题的轻量规划
- `deep_research_*` 负责高级、异步、报告型深度研究任务
- `get_sources` 负责把来源从“答案里的链接”升级成结构化可读取的信源数据

当前推荐主路径是 `plan_* -> web_search`，并在需要来源核对时按需调用 `get_sources`；对明确单跳、低歧义、规划收益很低的查询，也允许直接调用 `web_search`。更重的深度探索能力继续收口到 `deep research`，并优先在 CLI 落地。

当前公开的 `stdio` 安装示例以维护中的发布仓库 `Boulea7/GrokSearchTool` 为准；本地开发工作区、历史远端命名或旧协作痕迹不代表项目仍按 `fork/upstream` PR 模式推进。

```text
Client / Assistant
  └─ MCP / companion skill
      └─ GrokSearch Server
          ├─ plan_*      -> 轻量规划层
          ├─ deep_research_* -> 异步 job 层
          ├─ web_search  -> Grok 主答案
          │                + Tavily supplemental search
          │                + Firecrawl supplemental search
          │                + Sources cache / get_sources
          ├─ web_fetch   -> Tavily Extract
          │                -> Firecrawl Scrape fallback
          └─ web_map     -> Tavily Map
```

### 项目定位

推荐分层：

- `plan_* -> web_search`：默认轻量研究路径；需要结构化来源时再调用 `get_sources`
- `web_fetch`：抓单页正文
- `web_map`：看站点结构
- `deep research`：更长时间、更强编排的高级研究层，当前提供非交互 MCP job surface，并由 CLI 承接更完整交互

### 核心价值

- **答案与来源分离**：`web_search` 返回正文，`get_sources` 返回结构化来源，便于后续核验、排序和复用
- **多 provider 协作**：Grok 负责主回答，Tavily 负责搜索控制 / 抓取 / 映射，Firecrawl 负责托底和补充
- **轻量规划优先**：复杂任务先走 `plan_*`，简单任务直接搜，避免无意义的重编排
- **深度研究独立分层**：高级研究走 `deep_research_*` 与 `grok-search-research`，不挤占默认轻路径
- **面向真实运维**：内置 `get_config_info`、feature readiness、最小真实探针、稳定错误契约
- **运行时安全边界**：对抓取/映射目标做 URL 边界收口，对诊断输出和来源 URL 做敏感信息遮罩
- **兼容 OpenAI 风格接入**：可对接 Grok-compatible 中转与镜像站，但实际兼容性仍取决于上游对 `/models` 与 `/chat/completions` 的实现

### 适用场景

- 让代码助手在回答前先查最新文档、API、规范或公告
- 对模型生成内容做来源核验，而不是只看“像不像对”
- 对某个网页做可靠正文抓取，而不是只拿搜索摘要
- 先列清复杂研究任务，再逐步执行
- 在支持内建网页工具路由的宿主里收口统一的网页工具入口


## 二、安装

### 前置条件

- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)（推荐的 Python 包管理器）
- 支持 `stdio` MCP 的客户端，如 Claude Code、Codex CLI、Cherry Studio

### 支持级别

- `Officially tested`：Claude Code（已按仓库内文档验证本地 `stdio` 路径与项目级设置路径，不代表完整宿主 E2E 矩阵）
- `Community-tested`：Codex 风格 MCP 客户端、Cherry Studio
- `Planned`：Dify、n8n、Coze

更广义的 MCP 宿主适配资产与宿主原生规则 / preset / skill 映射，统一收口在 [docs/HOSTS.md](./docs/HOSTS.md)。这些资产用于帮助 Cursor、Cline、Continue、Windsurf、Cherry Studio 等宿主更稳定地接入本项目，但不单独改变上面的 support level 声明。

说明：

- 公开安装文档当前只承诺本地 `stdio` 路径
- `toggle_builtin_tools` 仅适用于 Claude Code 项目级设置
- `get_config_info` 中 `toggle_builtin_tools` 的 readiness 仅表示检测到了本地 Git 项目上下文，不代表已经完成完整的 Claude Code 宿主验证
- 下面的安装片段默认使用当前维护中的公开安装源 `Boulea7/GrokSearchTool`

<details>
<summary><b>安装 uv</b></summary>

```bash
# Linux/macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

> Windows 用户**强烈推荐**在 WSL 中运行本项目。

</details>

### 一键安装
若之前安装过本项目，使用以下命令卸载旧版MCP。
```
claude mcp remove grok-search
```


将以下命令中的环境变量替换为你自己的值后执行。Grok 接口需为 OpenAI 兼容格式；Tavily 为可选配置，未配置时 `web_map` 不可用；若仅配置 Firecrawl，`web_fetch` 仍可用。

```bash
claude mcp add-json grok-search --scope user '{
  "type": "stdio",
  "command": "uvx",
  "args": [
    "--from",
    "git+https://github.com/Boulea7/GrokSearchTool@main",
    "grok-search"
  ],
  "env": {
    "GROK_API_URL": "https://api.example.com/v1",
    "GROK_API_KEY": "your-grok-api-key",
    "TAVILY_API_KEY": "tvly-your-tavily-key",
    "TAVILY_API_URL": "https://api.tavily.com",
    "FIRECRAWL_API_KEY": "fc-your-firecrawl-key"
  }
}'
```

### 其他 `stdio` 客户端最小配置

#### Codex CLI / Codex 风格 MCP 客户端

将以下片段加入 `~/.codex/config.toml` 或项目级 `.codex/config.toml`：

```toml
[mcp_servers.grok-search]
command = "uvx"
args = ["--from", "git+https://github.com/Boulea7/GrokSearchTool@main", "grok-search"]

[mcp_servers.grok-search.env]
GROK_API_URL = "https://api.example.com/v1"
GROK_API_KEY = "your-grok-api-key"
TAVILY_API_KEY = "tvly-your-tavily-key"
TAVILY_API_URL = "https://api.tavily.com"
FIRECRAWL_API_KEY = "fc-your-firecrawl-key"
```

若使用项目级 `.codex/config.toml`，建议不要直接把真实 key 提交到仓库；当前仓库默认忽略 `.codex/`。本地开发更推荐把敏感变量写入已忽略的 `.env.local`。

`grok-search` 运行时会按“进程环境 > 项目 `.env.local` > 项目 `.env` > 持久化配置 > 代码默认值”自动读取配置，因此通常不需要再把 `.env.local` 当作 shell 脚本去 `source`。项目级环境变量回退当前同时支持普通 dotenv 形式的 `KEY=value` 与可选 `export KEY=value` 前缀；如果你确实需要把变量导出到当前 shell，请只对 shell-safe 的 env 文件使用显式导出方案。

如果会调用 `toggle_builtin_tools`，还应避免提交项目级 `.claude/settings.json`；当前仓库默认忽略 `.claude/`。

#### Cherry Studio

在 Cherry Studio 的 MCP 配置中新增一个 `STDIO` server，核心字段保持如下：

```json
{
  "name": "grok-search",
  "type": "stdio",
  "command": "uvx",
  "args": ["--from", "git+https://github.com/Boulea7/GrokSearchTool@main", "grok-search"],
  "env": {
    "GROK_API_URL": "https://api.example.com/v1",
    "GROK_API_KEY": "your-grok-api-key",
    "TAVILY_API_KEY": "tvly-your-tavily-key",
    "TAVILY_API_URL": "https://api.tavily.com",
    "FIRECRAWL_API_KEY": "fc-your-firecrawl-key"
  }
}
```

<details> <summary>如果遇到 SSL / 证书验证错误</summary>

在部分企业网络或代理环境中，可能会出现类似错误：

```text
certificate verify failed
self signed certificate in certificate chain
```

可以在 `uvx` 参数中添加 `--native-tls`，让安装/启动过程使用系统证书库。它适合处理企业代理、自签证书或系统证书链问题，不应被理解为通用的运行时 `verify=false` 替代方案：

```bash
claude mcp add-json grok-search --scope user '{
  "type": "stdio",
  "command": "uvx",
  "args": [
    "--native-tls",
    "--from",
    "git+https://github.com/Boulea7/GrokSearchTool@main",
    "grok-search"
  ],
  "env": {
    "GROK_API_URL": "https://api.example.com/v1",
    "GROK_API_KEY": "your-grok-api-key",
    "TAVILY_API_KEY": "tvly-your-tavily-key",
    "TAVILY_API_URL": "https://api.tavily.com",
    "FIRECRAWL_API_KEY": "fc-your-firecrawl-key"
  }
}'
```
</details>

除此之外，你还可以在`env`字段中配置更多环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `GROK_API_URL` | 是 | - | Grok API 地址（OpenAI 兼容格式，推荐显式包含 `/v1` 后缀；代码层不会仅因省略 `/v1` 就预先拦截，但多数 OpenAI 兼容端点仍可能因此在运行时失败，并通常伴随兼容性 warning） |
| `GROK_API_KEY` | 是 | - | Grok API 密钥 |
| `GROK_MODEL` | 否 | `grok-4.20-0309` | 默认模型；优先级见下方说明（进程 env > 项目 `.env.local` > 项目 `.env` > 持久化 config > 代码默认值） |
| `GROK_MODEL_PROFILE` | 否 | `balanced_auto` | 当未显式设置 `GROK_MODEL` 时，按统一工具级策略解析默认模型；provider 只影响 suffix、endpoint path 与兼容性诊断 |
| `GROK_DEEP_RESEARCH_STANDARD_PROFILE` | 否 | `reasoning` | `deep research` 的 `standard` 档默认 profile；当前默认主模型为 `grok-4.20-expert`，不可用时按统一 fallback 顺序自动回落 |
| `GROK_DEEP_RESEARCH_DEEP_PROFILE` | 否 | `multi_agent` | `deep research` 的 `deep` 档默认 profile；若当前 provider / relay / 账户不支持多 agent，会自动回落到单 agent |
| `GROK_DEEP_RESEARCH_ULTRA_PROFILE` | 否 | `ultra` | `deep research` 的 `ultra` 档默认 profile；显式请求时优先尝试 `grok-4.20-heavy-16-agent`，不可用时会自动降级到更轻的多 agent / 单 agent 模型 |
| `GROK_API_URL_2` / `GROK_API_KEY_2` / `GROK_MODEL_2` | 否 | - | 第 2 个 Grok 供应商；当主供应商失败时会自动切到该供应商继续重试 |
| `GROK_API_URL_3+` / `GROK_API_KEY_3+` / `GROK_MODEL_3+` | 否 | - | 更多 Grok 供应商，按编号升序依次作为 failover provider chain |
| `GROK_MODEL_FALLBACKS` | 否 | 内建降级链 | 逗号分隔的模型自动降级顺序；当当前供应商明确返回“模型不可用”时，会在同一供应商内按该顺序继续尝试，例如可显式包含 `grok-4.1-fast` |
| `GROK_WEB_SEARCH_MODEL` | 否 | `grok-4.20-fast` | `web_search` 的工具级默认主模型；默认不再隐式继承全局 `GROK_MODEL` |
| `GROK_WEB_SEARCH_FALLBACK_MODELS` | 否 | `grok-4.20-0309,grok-4.20-auto,grok-4.20-0309-non-reasoning,grok-4.20-reasoning,grok-4.20-expert` | `web_search` 的工具级 fallback 顺序；provider 只补 suffix / path / diagnostics，不改这组顺序 |
| `GROK_TIME_CONTEXT_MODE` | 否 | `always` | 时间上下文注入策略：`always` / `auto` / `never` |
| `TAVILY_API_KEY` | 否 | - | Tavily API 密钥（用于 `web_fetch` / `web_map`，也用于 Tavily supplemental `web_search`） |
| `TAVILY_API_URL` | 否 | `https://api.tavily.com` | Tavily API 地址 |
| `TAVILY_ENABLED` | 否 | `true` | 是否启用 Tavily |
| `TAVILY_FALLBACK_API_URL` | 否 | `https://tavily-fallback.example.com/api/tavily` | 当主 Tavily 地址是本机 loopback 且不可用时使用的远端 HTTP API fallback 地址 |
| `TAVILY_FALLBACK_API_KEY` | 否 | 复用 `TAVILY_API_KEY` | Tavily fallback Bearer token；不要写入公开仓库 |
| `TAVILY_FALLBACK_ENABLED` | 否 | `true` | 是否允许本机 Tavily 端口失败后尝试 fallback |
| `FIRECRAWL_API_KEY` | 否 | - | Firecrawl API 密钥（用于 `web_fetch` 托底，也可用于 supplemental `web_search`） |
| `FIRECRAWL_API_URL` | 否 | `https://api.firecrawl.dev/v2` | Firecrawl API 地址 |
| `GROK_DEBUG` | 否 | `false` | 调试模式；同时控制 debug-only 进度日志与 `ctx.info()` 中间进度转发 |
| `GROK_LOG_LEVEL` | 否 | `INFO` | 日志级别 |
| `GROK_LOG_DIR` | 否 | `logs` | 日志目录；`get_config_info` 中展示的是解析后的运行时绝对路径 |
| `GROK_OUTPUT_CLEANUP` | 否 | `true` | 是否启用 `web_search` 输出清洗 |
| `GROK_FILTER_THINK_TAGS` | 否 | 兼容别名 | `GROK_OUTPUT_CLEANUP` 的旧别名，优先推荐配置 `GROK_OUTPUT_CLEANUP` |
| `GROK_RETRY_MAX_ATTEMPTS` | 否 | `3` | 最大重试次数 |
| `GROK_RETRY_MULTIPLIER` | 否 | `1` | 重试退避乘数 |
| `GROK_RETRY_MAX_WAIT` | 否 | `10` | 重试最大等待秒数 |
| `GROK_DEEP_RESEARCH_DIR` | 否 | `~/.config/grok-search/deep-research` | deep research SQLite 状态与 artifacts 根目录 |
| `GROK_DEEP_RESEARCH_DEFAULT_BUDGET_SECONDS` | 否 | `240` | deep research 默认目标预算秒数 |
| `GROK_DEEP_RESEARCH_HARD_TIMEOUT_SECONDS` | 否 | `600` | deep research 硬超时上限 |
| `GROK_DEEP_RESEARCH_MAX_CONCURRENCY` | 否 | `3` | 默认 deep research runtime 同一轮 ready research unit 的最大并发执行数 |
| `GROK_DEEP_RESEARCH_RECENT_REUSE_SECONDS` | 否 | `1800` | 已完成 deep research job 按 `finished_at` 参与复用的时间窗口 |
| `PYTHONIOENCODING` | 否 | `utf-8` | 建议显式设为 UTF-8，减少 Windows / 中转站日志乱码 |
| `PYTHONUNBUFFERED` | 否 | `1` | 关闭 Python stdout 缓冲，减少 stdio MCP 启动卡顿 |
| `PYTHONUTF8` | 否 | `1` | 强制 Python UTF-8 模式 |

> 模型解析优先级为：进程里的 `GROK_MODEL` > 项目 `.env.local` > 项目 `.env` > `~/.config/grok-search/config.json` 中由 `switch_model` 持久化的值 > 代码默认值 `grok-4.20-0309`。如使用 OpenRouter 兼容地址，运行时还会自动补齐 `:online` 后缀。

> 环境变量优先级按“是否存在”判断：只要进程环境里显式设置了某个键，即使值为空字符串，也不会再回落到项目 `.env.local` / `.env`。
>
> 若配置了 `GROK_API_URL_2` / `GROK_API_KEY_2` 及更高编号的同名变量，运行时会把它们视为备用 Grok 供应商链路：当前一个供应商在真实请求阶段失败时，会自动按编号顺序切换到下一个继续尝试，直到命中可用供应商或全部失败为止。
>
> 若配置了 `GROK_MODEL_FALLBACKS`，当某个供应商明确返回“模型不可用”时，运行时会在同一供应商内按你给出的模型顺序继续尝试；未显式配置时，会使用内建的 Grok 4.20 -> `grok-4.1-fast` 等降级链。

> `get_config_info` 的基础配置快照当前会额外返回 `GROK_MODEL_SOURCE`，用于标识当前活动模型来自哪一层（如 `process_env`、`project_env_local`、`project_env`、`persisted_config`、`default`）。如果这里显示的是 `process_env` 或 `project_env_local` / `project_env`，单独调用 `switch_model` 不会改变当前进程，需先修改对应覆盖层。

> 当前默认首选模型是 `grok-4.20-0309`。运行时模型选择对 Grok 4.1+ 族会保持弹性：如果显式或隐式请求的模型不在 `/models` 返回列表里，但列表中存在兼容的 Grok 4.1+ 可用模型，系统会优先回退到更合适的可用模型，而不是仅因后缀不匹配而直接失败。

> 当没有显式 `GROK_MODEL` 时，运行时当前会按统一工具级策略解析默认模型；同一工具的主模型与 fallback 顺序不会因 provider family 改变。provider 当前只影响 `:online` suffix、`/chat/completions` vs `/responses` 路径选择，以及 capability / availability / diagnostics metadata。`get_config_info` 基础快照会额外返回 `GROK_MODEL_PROFILE`、`GROK_DEEP_RESEARCH_STANDARD_PROFILE`、`GROK_DEEP_RESEARCH_DEEP_PROFILE`、`GROK_DEEP_RESEARCH_ULTRA_PROFILE` 与 `GROK_PROVIDER_FAMILY`，便于排查兼容层差异。

> `get_config_info` 的基础快照现在还会返回 `GROK_ROUTING_DIAGNOSTICS`。其中会列出当前 active provider、编号 provider chain、各 profile 解析出的默认模型、预期会走 `/chat/completions` 还是 `/responses`，以及 multi-agent 相关 routing signals，方便判断官方 xAI、OpenRouter、普通 relay 与 `grok2api` 风格反代在当前配置下到底会怎么走。

> 当前 Grok 路由已支持 `/chat/completions` 与 `/responses` 双通道。多 agent 家族与部分 response-only relay 模型会优先走 `/responses`；OpenRouter 与多数兼容 relay 仍优先 `chat/completions`。

> `web_search` 当前默认采用独立的工具级模型策略：主模型默认 `grok-4.20-fast`，fallback 顺序为 `grok-4.20-0309 -> grok-4.20-auto -> grok-4.20-0309-non-reasoning -> grok-4.20-reasoning -> grok-4.20-expert`。如果没有显式设置 `GROK_WEB_SEARCH_MODEL` / `GROK_WEB_SEARCH_FALLBACK_MODELS`，这组顺序不会因为全局 `GROK_MODEL` 或 provider family 改变。

> `deep research` 当前默认采用统一工具级模型策略：`standard` 默认主模型 `grok-4.20-expert`，fallback 为 `grok-4.20-reasoning -> grok-4.20-auto -> grok-4.20-fast`；`deep` 默认主模型 `grok-4.20-expert-4-agent`，fallback 为 `grok-4.20-expert -> grok-4.20-multi-agent -> grok-4.20-reasoning`；`ultra` 默认主模型 `grok-4.20-heavy-16-agent`，fallback 为 `grok-4.20-heavy -> grok-4.20-expert-4-agent -> grok-4.20-expert -> grok-4.20-reasoning`。provider / relay 不改变同一工具的工具层策略，只负责路径、suffix 与兼容性落地。多 agent 通常会带来更高时延，单次大约可能在 `10` 秒到 `2` 分钟之间，`ultra` 通常还会更慢。

> `GROK_TIME_CONTEXT_MODE` 默认是 `always`，保持当前“全量注入本地时间上下文”的行为；如需节省上下文，可改为 `auto` 或 `never`。

经验建议：

- `GROK_API_URL` 推荐使用带显式 `/v1` 后缀的 OpenAI 兼容根路径；代码层不会仅因省略 `/v1` 就预先拦截，但多数 OpenAI 兼容端点仍可能因此在运行时失败，并通常伴随兼容性 warning
- `web_search` 调用时若没有用户明确指定模型，尽量不要传 `model` 参数，否则会覆盖默认的 `GROK_MODEL`
- 如需更省上下文，可将 `GROK_TIME_CONTEXT_MODE` 设为 `auto`（只在明显时效查询或显式时效控制下注入）或 `never`
- `GROK_DEBUG=false` 时，`log_info()` 不会写入这类 helper 日志，也不会通过 `ctx.info()` 暴露中间进度；仅在 `GROK_DEBUG=true` 时转发 debug-only progress
- redirect preflight 若因超时或请求级错误失败，`web_fetch` / `web_map` 当前会直接 fail-closed 并阻断下游 provider 调用；`skipped_due_to_error` 仅保留为内部诊断 / 兼容性 reason code，不再作为继续执行路径
- 若 `content` 为空，先检查中转站是否真的返回了正文；若 `sources_count=0`，再检查是否提供了结构化 citations，或正文里是否至少包含可解析的 Markdown 链接 / 裸 URL
- 若上游 endpoint 指向 `localhost` / `127.x` 等 loopback 地址，运行时会对该请求强制 `trust_env=False`，因此会一并绕过 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` 以及 `SSL_CERT_FILE` / `SSL_CERT_DIR`


### 验证安装

```bash
claude mcp list
```

### 最小 smoke check

无论你使用 Claude Code、Codex CLI 还是 Cherry Studio，建议至少做以下本地 `stdio` 验证：

1. 先调用 `get_config_info`，确认基础配置快照、`connection_test`、`doctor` 与 `feature_readiness` 符合你的安装目标；可选的 `search/fetch` 探针在未配置对应 provider 时允许跳过或显示 `not_ready`
2. 再调用一次 `web_search`，验证主搜索链路可用
3. 若需要引用核对，再调用 `get_sources`
4. 配置了 Tavily 或 Firecrawl 后再验证 `web_fetch`；仅在配置并启用 Tavily 后再验证 `web_map`

若你使用的是 Claude Code，且希望优先使用本 MCP 的搜索实现，可额外调用：
```text
调用 grok-search toggle_builtin_tools，关闭 Claude Code 的 built-in WebSearch 和 WebFetch tools
```

该工具会修改**项目级** `.claude/settings.json` 的 `permissions.deny`，以项目级 deny 规则收敛 Claude Code 内建网页工具的可用性。它只对 Claude Code 项目设置生效，不应被理解为其他 host 的通用能力，也不保证所有搜索路径都会被强制改写。



## 三、MCP 工具介绍

<details>
<summary>本项目提供 MCP 工具（展开查看）</summary>

### `web_search` — AI 网络搜索

通过 Grok API 执行 AI 驱动的网络搜索，默认仅返回 Grok 的回答正文，并返回 `session_id` 以便后续获取信源。

`web_search` 输出不展开完整信源，仅返回 `sources_count` 与结构化状态字段；信源会按 `session_id` 暂存于当前服务器进程内的内存型 LRU 缓存中，可用 `get_sources` 拉取。

默认推荐对非显而易见的单跳任务先走 `plan_* -> web_search`；如果查询本身已经足够明确、低歧义，且 planning 只会增加摩擦，则可直接调用 `web_search`。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | 是 | - | 搜索查询语句 |
| `platform` | string | 否 | `""` | 聚焦平台（如 `"Twitter"`, `"GitHub, Reddit"`） |
| `model` | string | 否 | `null` | 按次指定 Grok 模型 ID |
| `extra_sources` | int | 否 | `0` | 额外补充信源数量（Tavily/Firecrawl，可为 0 关闭） |
| `topic` | string | 否 | `"general"` | 补充搜索主题，目前支持 `general` / `news` / `finance` |
| `time_range` | string | 否 | `null` | 相对时间范围，目前支持 `day` / `week` / `month` / `year`，并兼容 `d` / `w` / `m` / `y` |
| `include_domains` | string[] | 否 | `[]` | Tavily 补充搜索白名单域名 |
| `exclude_domains` | string[] | 否 | `[]` | Tavily 补充搜索黑名单域名 |

默认会注入本地时间上下文，以提升时效性搜索的准确度；可通过 `GROK_TIME_CONTEXT_MODE=always|auto|never` 调整。
若补充搜索走 Tavily，`max_results` 当前会自动收敛到 Tavily 文档给出的上限 `20`。

返回值（结构化字典）：
- `session_id`: 本次查询的会话 ID
- `content`: Grok 回答正文（已自动剥离信源）
- `sources_count`: 已缓存的信源数量
- `status`: `ok` / `partial` / `error`
- `effective_params`: 最终生效的搜索参数回显
- `warnings`: 非致命告警列表；例如 Tavily 不可用时，域名过滤和时间范围不会真正作用于补充搜索，或上游只返回信源列表 / 正文疑似截断时返回 `body_missing_sources_only`、`body_probably_truncated`
- `error`: 稳定的机器可读错误码；无错误时为 `null`

说明：
- `topic`、`time_range`、`include_domains`、`exclude_domains` 当前是 Tavily-backed supplemental search 的能力；如果本次请求没有实际走 Tavily 补充搜索，主请求仍可继续执行，但这些控制项不会真正生效，并会通过 `warnings` 或 `partial` 状态体现。
- 若上游只返回信源列表而没有正文，或正文命中当前的截断启发式，`web_search` 当前也会返回 `partial`；其中 `get_sources.search_status` 会同步保留该降级状态，`get_sources.search_warnings` 会回放与该搜索会话绑定的 warning code。

### `get_sources` — 获取信源

通过 `session_id` 获取对应 `web_search` 的全部信源。

当前 `get_sources` 使用的是当前服务器进程内的内存型 LRU 缓存：默认 TTL 约 1 小时、当前上限 256 个 session。进程重启、TTL 到期或缓存淘汰后，先前的 `session_id` 会失效。

`session_id` 当前只是运行中 server 进程里的 shared-daemon transient handle，不是 durable、caller-bound capability，也不是 secret token；只要在同一个运行中进程里持有该值，就能继续读取对应信源。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | 是 | `web_search` 返回的 `session_id` |

返回值（结构化字典）：
- 成功时返回 `session_id`、`sources_count`、`sources`；其中每个 source 至少包含 `title`、`url`、`provider`、`source_type`、`snippet`、`domain`、`score`、`published_at`、`retrieved_at`、`rank`，并会在可用时以 additive 方式补充如 `origin_type`、`contributors` 这类 provenance 字段
- 成功时还会返回 `search_status`、`search_error`、`search_warnings`、`source_state`，用于区分成功但无信源、部分成功和失败，并回放当前会话缓存中的非致命 warning code
- miss 时返回 `session_id`、`sources=[]`、`sources_count=0`
- `error`: 仅在 `session_id` 缺失或过期时返回，例如 `session_id_not_found_or_expired`

说明：
- `session_id_not_found_or_expired` 当前统一覆盖进程重启、TTL 到期、LRU 淘汰，以及不可读的旧缓存条目等 miss 场景。
- `search_warnings` 当前会回放与该搜索会话绑定的 warning code；旧缓存条目若没有该字段，则默认返回空数组。
- `sources_count` 当前等于标准化、去重与过滤之后最终写入缓存的信源数量，不等于上游原始 citations 条数。
- 去重后的单条 source 当前应被理解为 normalized aggregate row：如果同一 URL 同时来自多个输入路径，`provider` 表示该聚合结果最终保留下来的 normalized winner provider，而 `source`、`origin_type` 等 provenance metadata 仍可能来自其他贡献行；只有当不同的 contributor identity 被折叠进同一行时，才会额外暴露 additive `contributors` 供调用方查看 contributor 级 attribution。
- `source` 当前仍带有 legacy 重载语义：当 `origin_type` 缺失时，它仍可能被当作旧缓存里的 provider alias 回填到 `provider`；只有在 `origin_type` 等 provenance 信号存在时，`source` 才更接近 provenance label。调用方不应把它当作无歧义字段。
- `rank` 当前会按 `score`、来源身份清晰度与稳定去重顺序生成，不再对 Grok 引用做额外优先级偏置。
- `standardize_sources` 当前会在去重时规范化 URL 的 scheme/host 大小写，因此同一页面的 mixed-case 变体可能折叠为一个 source；同时会保留普通锚点（fragment），避免不同页面段落引用被误合并，并继续剥离 URL `userinfo`、遮罩常见 query / fragment 签名参数，以及常见 OAuth/OIDC credential 参数（如 `client_secret`、`refresh_token`、`id_token`、`password`）。高置信度 cloud-signed credential 键（如 `X-Amz-Credential`、`X-Goog-Credential`、`GoogleAccessId`）当前也属于遮罩范围。显式默认端口（如 `:443` / `:80`）当前仍会保留，不会与隐式默认端口 URL 自动折叠。
- 默认不会把裸 `auth` / `key` 这类宽泛参数名一并视为敏感字段；当前 masking 仍优先收口到高置信度 credential / 签名键，避免误伤普通诊断信息、示例 URL 与可核验 source 链接。
- `get_sources` 生命周期与共享 daemon 边界详见 [docs/GET_SOURCES_LIFECYCLE.md](./docs/GET_SOURCES_LIFECYCLE.md)。

### `web_fetch` — 网页内容抓取

通过 Tavily Extract API 获取网页正文，返回提取后的 Markdown 文本。Tavily 失败时自动降级到 Firecrawl Scrape 进行托底抓取。

说明：
- 当前 `web_fetch` / `web_map` / Tavily supplemental `web_search` 只暴露 provider 能力的一个受控子集，不等同于 Tavily / Firecrawl 全量原生参数面。
- `web_fetch` 返回的是提取后的 Markdown 文本，不会透传 provider 的原始结构化响应字段。
- `web_fetch` / `web_map` 默认拒绝非 `http/https`、loopback、明显私网目标、单标签主机名、常见私网后缀主机（如 `.internal` / `.local` / `.lan` / `.home` / `.corp`）、常见 loopback helper 域名（如 `localtest.me` / `lvh.me`），以及常见把私网 IP 编进公网 DNS 名的 alias 形态（如 `nip.io` / `xip.io` / `sslip.io`）。
- 对通过静态校验的目标，`web_fetch` / `web_map` 还会在真正调用 provider 前继续复检可见的 redirect 目标。
- 当前可见 redirect 复检使用 `GET` 请求而不是 `HEAD`；对 presigned URL、one-shot token 或有副作用的读取型链接，这意味着可能存在额外一次预检读取，应视为已知边界。
- 当前可见 redirect 复检最多会发起 `5` 次预检请求；如果到第 `5` 次预检时仍然看到新的可见重定向，就会直接返回“目标 URL 重定向次数过多”并拒绝继续调用下游 provider。
- 若 redirect 预检发生超时或请求级错误，当前实现会直接返回失败并阻断下游 provider 调用；`skipped_due_to_error` 仅作为内部诊断 reason code 保留，不再代表“继续执行下游 provider”。
- 当前这层边界依然不会仅因本机 DNS 把某个看似公网的 hostname 解析到私网就直接拒绝请求，因此它仍不应被理解为对 split-horizon / 本地 DNS 私有解析的强保证；但对可见 redirect 失败路径已经收紧为 hard-stop。
- 当前实现为了避免误杀普通公网 hostname，不会因为本机 DNS 把某个公网域名解析到私网结果就直接拒绝请求；因此这层边界不应被理解为对 split-horizon / 本地 DNS 私有解析的强保证。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 目标网页 URL |

### `web_map` — 站点结构映射

通过 Tavily Map API 遍历网站结构，发现 URL 并生成站点地图。

说明：
- Tavily Map 默认可能返回外部域名链接；若你需要更接近站内 sitemap 的结果，请结合 `instructions` 收紧范围，并按返回结果自行过滤。当前文档中的这条说明对应 Tavily 文档中 `allow_external=true` 的默认行为，本封装暂未直接暴露该开关。
- 默认会拒绝非 `http/https`、loopback、明显私有网络目标、单标签主机名、常见私网后缀主机、常见 loopback helper 域名（如 `localtest.me` / `lvh.me`），以及常见把私网 IP 编进公网 DNS 名的 alias 形态，并在调用 Tavily 前继续做可见 redirect 目标复检。
- 上述边界同样覆盖明显的 private / 私有网络目标；当前策略优先阻断这类目标，再决定是否继续调用下游 provider。
- 可见 redirect 复检当前使用 `GET` 而不是 `HEAD`；最多会发起 `5` 次预检请求，如果到第 `5` 次预检时仍然看到新的可见重定向，就会返回“目标 URL 重定向次数过多”并拒绝继续执行下游 provider。若预检超时或发生请求级错误，当前也会直接 fail-closed；`skipped_due_to_error` 仅保留为内部 reason code，不再继续执行下游 provider。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `url` | string | 是 | - | 起始 URL |
| `instructions` | string | 否 | `""` | 自然语言过滤指令 |
| `max_depth` | int | 否 | `1` | 最大遍历深度（1-5） |
| `max_breadth` | int | 否 | `20` | 每页最大跟踪链接数（1-500） |
| `limit` | int | 否 | `50` | 总链接处理数上限（1-500） |
| `timeout` | int | 否 | `150` | 超时秒数（10-150） |
| `response_format` | string | 否 | `"json_string"` | 返回格式：`"json_string"` 保持 legacy JSON 字符串 / 文本错误契约，`"object"` 则优先返回结构化对象 |

推荐新调用方显式传 `response_format="object"`，也就是使用 `object` 模式，这样成功结果可直接拿到对象；默认仍保持 legacy JSON 字符串兼容模式。对 `web_map` 来说，object 模式会把原来的 JSON 字符串结果直接解成对象；若命中旧式纯文本错误，则会返回带 `status="error"` 和 `message` 的对象。

### `get_config_info` — 配置诊断

支持可选参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `detail` | string | 否 | `"full"` | 返回级别：`"full"` 保留完整 doctor/probe 细节，`"summary"` 仅保留高频机器可读摘要 |

`Config.get_config_info()` 只负责返回基础配置快照；MCP 工具 `get_config_info` 会保留该快照，并由 server 层补充诊断结果，返回：

- Grok `/models` 连通性与可用模型
- Tavily / Firecrawl 的只读探测结果（仅在已配置时执行）
- 默认最小真实 `web_search` / `web_fetch` 探针结果
- `web_search` / `get_sources` / `web_fetch` / `web_map` / `toggle_builtin_tools` / `deep_research_planner` / `deep_research_runtime` 的 readiness 汇总
- 修复建议列表（API Key 自动脱敏）
- `doctor.recommendations_detail`：与 `check_id` / `feature` 关联的结构化修复建议
- `feature_readiness.web_fetch.providers`：provider 级状态，稳定包含 `check_id`；`verified_path` 表示真实抓取探针实际打通的后端；未执行或退化的 provider 会在可判定时附带 `reason_code`，并可能补充 `skipped_reason`
- `grok_provider_chain`：当前解析到的 Grok provider 数量、命名摘要，以及每个 provider 的 family / resolved model / 预期 endpoint path
- 基础快照里的 `GROK_ROUTING_DIAGNOSTICS`：当前 active provider、provider chain、按 profile 推导出的默认模型、`/chat/completions` 与 `/responses` 路径可见性，以及 multi-agent routing signals
- 基础快照里的 `GROK_MODEL_SOURCE`：当前活动模型的来源层，便于区分是进程 env、项目 `.env.local` / `.env`、持久化配置还是代码默认值在生效

注意：
- `detail="full"` 保留完整 `doctor.checks`、`doctor.recommendations_detail` 和 provider/probe 细节；`detail="summary"` 只保留基础配置快照、`connection_test`、`doctor.status/summary/recommendations` 与 `feature_readiness`
- `detail="summary"` 当前只是同一次诊断结果的紧凑字段投影，不是额外的“轻执行路径”；底层仍会执行同一轮配置/探针逻辑。
- `connection_test` 当前只反映 `/models` 连通性，不代表当前活动模型一定能通过真实 `chat/completions` 路径；判断 `web_search` 是否真可用时，应结合 `doctor`、`feature_readiness`、`GROK_MODEL_SOURCE` 与 `grok_model_selection` / `grok_model_runtime_fallback` / `grok_search_probe` 结果一起看。
- `grok_model_selection` 表示 `/models` 列表阶段就已发现当前模型不可直接使用，并会在运行前预选到更合适的 Grok 候选模型；`grok_model_runtime_fallback` 表示当前 probe model 在真实 `chat/completions` 路径上仍只能靠运行时二次回退才成功。这两个 check 可能同时出现。
- `grok_search_probe` 当前除了 `ok` / `error` 之外，也可能返回正文质量降级类 `warning`；例如探针只拿到信源列表、没有可用正文，或正文疑似截断时，`feature_readiness.web_search` 会相应显示为 `degraded`。
- `grok_search_probe` 当前在成功时还会附带实际命中的 `provider_name` / `provider_model`，并按该模型的真实 routing 规则回显 endpoint path；当 secondary provider 接住请求或 multi-agent 模型改走 `/responses` 时，diagnostics 会按真实 winner 回显，而不是只停留在 primary 静态配置层。
- 运行时模型回退当前属于 best-effort 兼容路径：它依赖 `/models` 能返回可选候选列表，且上游错误摘要命中“模型不可用”类文案；如果 `/models` 不可用，或错误类型不属于该类信号，就不保证会自动继续回退。
- `feature_readiness.get_sources` 只有在当前进程内至少存在一个非 error 的可读取 source session 时才会显示 `ready`；如果只有失败搜索留下的 session，状态会保持 `partial_ready`。即使 `web_search` 当前尚未 ready，只要当前进程里仍保有可读取 session，`get_sources` 也会继续显示 `ready`，同时通过 `degraded_by` 暴露上游配置问题。
- `feature_readiness.get_sources` 当前会附带 `cache_summary`，至少包含 `total_sessions`、`readable_sessions`、`error_sessions`、`partial_sessions`、`unreadable_sessions`，用于快速判断当前 source cache 的可读性与退化面。
- `feature_readiness` 当前还会提供一组 summary-safe 机器字段：`based_on_checks` 表示该能力主要参考了哪些 doctor checks，`probe_scope` 表示结论属于哪类探针/状态面，`degraded_by` 用 `check_id/status/reason_code` 描述当前退化来源。对 `get_sources`，cache 侧退化当前会使用 synthetic cause `source_cache_state`；对 `web_search` 还会额外返回 `runtime_override_active` 与 `runtime_model_source`，用于标记当前退化是否受进程 env / 项目 `.env.local` / `.env` 覆盖层影响。
- `feature_readiness.deep_research_planner` 与 `feature_readiness.deep_research_runtime` 当前会额外暴露 `profile_probes`，分别给出 `standard` / `deep` / `ultra` 三档 deep research 的真实探针结果、winning provider / model，以及命中的 endpoint path，用于区分“默认轻路径正常”与“指定档位的 deep research 也能真实工作”。
- `feature_readiness` / `doctor` 的状态语义当前可按以下方式理解：`ready`=当前能力已验证可用，`degraded`=能力存在但探针或局部依赖异常，`not_ready`=配置或前置条件不足，`partial_ready`=接口存在但仍缺少运行中瞬时条件；其中 `transient` 和 `client_specific` 项默认不拉低 overall doctor。
- 输出中的 API Key 会脱敏；显而易见的 bearer/token/签名 query、常见 OAuth/OIDC credential 参数，以及高置信度 cloud-signed credential 键（如 `X-Amz-Credential`、`X-Goog-Credential`、`GoogleAccessId`）也会做遮罩。但诊断结果仍可能包含本机绝对路径、endpoint/主机名或精简后的上游错误摘要；若要贴到 issue / 聊天，请先二次检查并按需删减。

### `switch_model` — 模型切换

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | 是 | 模型 ID（如 `"grok-4-fast"`, `"grok-2-latest"`） |
| `response_format` | string | 否 | 返回格式：`"json_string"` 保持 legacy JSON 字符串契约，`"object"` 直接返回原本 JSON payload 对应的结构化对象 |

切换后配置持久化到 `~/.config/grok-search/config.json`，跨会话保持。
若当前进程或项目 `.env.local` / `.env` 已显式设置 `GROK_MODEL`，`switch_model` 仍会写入持久化配置，但当前进程的实际生效模型不会立刻改变。
当返回里 `runtime_model_source` 显示为 `process_env`、`project_env_local` 或 `project_env` 时，应先修改对应覆盖层；单独调用 `switch_model` 不会改变当前进程。
建议新调用方显式传 `response_format="object"`，也就是使用 `object` 模式，避免再做一次 `json.loads(...)`；默认仍保持 legacy JSON 字符串兼容模式。

### `toggle_builtin_tools` — 工具路由控制

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `action` | string | 否 | `"status"` | `"on"` 禁用官方工具 / `"off"` 启用官方工具 / `"status"` 查看状态 |
| `response_format` | string | 否 | `"json_string"` | 返回格式：`"json_string"` 保持 legacy JSON 字符串契约，`"object"` 直接返回结构化对象 |

通过修改项目级 `.claude/settings.json` 的 `permissions.deny`，为 Claude Code 添加或移除内建网页工具的 deny 规则。
建议新调用方显式传 `response_format="object"`，也就是使用 `object` 模式；默认仍保持 legacy JSON 字符串兼容模式。

稳定错误码：
- `git_root_not_found`：当前目录不在可识别的 Git 项目里，无法定位项目级 `.claude/settings.json`
- `settings_file_invalid`：`.claude/settings.json` 存在但 JSON / 结构不合法
- `settings_write_failed`：项目设置文件写回失败
- `invalid_action`：`action` 不是 `on` / `off` / `status`

### `plan_intent` / `plan_complexity` / `plan_sub_query` / `plan_search_term` / `plan_tool_mapping` / `plan_execution`

结构化搜索规划脚手架（分阶段、多轮），用于在执行复杂搜索前先生成可执行的搜索计划。
其中 `plan_search_term` 在首次建立 `search_strategy` 时设置 `approach` / `fallback_plan`；后续非 `is_revision` 调用只会追加 `search_terms`，不会隐式改写既有 strategy metadata。

说明：
- 推荐阶段顺序为 `plan_intent -> plan_complexity -> plan_sub_query -> plan_search_term -> plan_tool_mapping -> plan_execution`。
- Level 1 planning 在 `query_decomposition` 后结束，Level 2 planning 在 `tool_selection` 后结束，Level 3 才会继续到 `execution_order`。
- `plan_*` wrapper 当前采用标量输入形态，例如 `depends_on` 使用 CSV、`parallel_groups` 使用分号分组的 CSV、`params_json` 使用字符串化 JSON；返回值会提供 `plan_complete`、`phases_remaining` 与 `executable_plan`，便于调用方直接承接下一步执行。
- `plan_*` 当前直接返回结构化对象，而不是 JSON 字符串；调用方不应再对工具返回值额外做一次 `json.loads(...)`。
- `plan_sub_query.boundary` 当前除了必填外，还会做最小机器校验：必须显式写出排除/不包含的边界语义，纯空泛描述不会通过。
- planning `session_id` 当前是进程内的 transient handle，默认 TTL 约 1 小时、LRU 上限 256；进程重启、TTL 到期或缓存淘汰后，应从新的 `plan_intent` session 重新开始。
- 首次建立 `search_strategy` 时必须提供 `approach`；只有在 strategy 已建立后，后续非 `is_revision` 调用才允许只追加 `search_terms`。
- 当 session 缺失、阶段顺序错误，或 revision 会破坏下游阶段时，当前会返回结构化错误，并明确要求从新 session 重新开始相应 planning 流程。

### `deep_research_start` / `deep_research_status` / `deep_research_events` / `deep_research_result` / `deep_research_artifact` / `deep_research_resume` / `deep_research_cancel` / `deep_research_list`

高级深度研究 job 层，适合多分钟、可恢复、可查询中间进度与 artifacts 的报告型任务。

说明：
- 这是高于 `plan_* -> web_search` 的高级层，不替代默认轻路径。
- `get_config_info(detail=summary)` 与默认 doctor 当前应优先反映轻路径主能力的健康状态；`deep_research_*` readiness 仍会保留在详细输出里，但属于高级可选层，不应单独把默认总体健康结论拉成 degraded。
- `deep_research_start` 会创建 job，并立即生成结构化 `plan.json`；其中至少包含 `brief`、`sub_questions`、`search_strategy`、`report_outline`、`research_units`。在 continuation 场景下，`plan.json` 里的 `continuation` 当前只保留 compact 摘要视图，完整 carry-forward state 会额外写到 `continuation.json`。非 `plan_only` 场景下，任务会异步推进并逐步产出 `partial_report.md`、`final_report.md`、`sources.json`、`citations.json`、`report.json`。
- `force_new` 用于控制 `deep_research_start` 是否必须创建全新 job。
- 当 `force_new=false` 且请求 fingerprint 与当前 `draft` / `queued` / `running` job 或复用窗口内的已完成 job 匹配时，`deep_research_start` 可能直接复用现有 job；这条规则当前同样适用于带 `continue_from_job_id` 的 follow-up job，返回里会通过 `reused` 明确标识。若调用方必须拿到全新 job，应显式传 `force_new=true`。当前 request fingerprint 也会区分 `plan_only`；`plan_only=true` 不应再复用 execution job，execution start 也不应复用 plan-only draft。对 `completed` job，当前只有在最终 provenance bundle 可读且核心 artifacts 与 sidecars 来自一致 `batch_id` 时才应继续被视为可复用结果；除 `sources.json`、`citations.json`、`report.json`、`final_report.md` 外，还会校验 `evidence_items.json`、`coverage.json`、`grounding.json`、`verifier.json` 以及可用的 additive sidecars。
- 若 `deep_research_start(force_new=false)` 命中的是 `interrupted` 且已存在可读 final artifact batch 的 job，当前也会先把它解析成 `completed` 再作为 reusable result 返回，避免与 `deep_research_resume` 对同一 job 给出不同终态语义。
- `deep_research_status` 返回 job、阶段、进度，以及带 `kind` / `path` / `content_type` / `updated_at` / `metadata.bytes` 的 artifact 摘要；对 `completed` job，还会额外返回当前是否正在读取 resolved final batch 的 additive 诊断字段，如 `artifact_fallback_used`、`resolved_artifact_batch_id`、`artifact_visibility_reason`。对 `interrupted` / `failed` / `canceled` 且 `current_checkpoint/phase` 已到 `finalizing` 的 job，只要存在一致且可读的 final provenance bundle，当前也应暴露同一批 resolved final artifacts。`evidence_items.json` 现在也会跟着 resolved final batch 一起出现在 `artifact_kinds` / `artifacts` 中；`evidence_bank.json` 与 `verification.json` 目前仍是 additive artifact，不参与 resolved batch 的硬门槛。`status` 现在还会暴露 additive attempt-window / operator 诊断字段，如 `watch_attach_after_seq`、`attempt_window_start_seq`、`partial_payload`，以及镜像这些字段的 `operator_summary`。CLI `grok-search-research result --artifact <kind>` 当前也应优先读取同一批 resolved final artifacts，而不是盲读当前指针。
- `deep_research_events` 返回有序事件流，支持 `after_seq` 增量读取；空 fetch/map 等显式 unit 失败当前会通过 `research_unit_failed` 暴露，而不应再静默计作 completed。
- `deep_research_result` 在 job 未完成时也可以返回当前 plan / partial artifacts；完成后 `final_report.md`、`sources.json`、`citations.json`、`report.json` 应保持一致。返回里的 `citations` 当前与 `citations.json` 保持完全同构，不再做隐式扁平化；`evidence_items` 也会作为独立字段一起返回。`report.json.sections` 现在还可能带 additive `prose`，`report.runtime` 还可能带 additive `verification`。Provider Accounting 当前也会作为最终报告正文与 `report.json` / `citations.json` 的 additive section 暴露，用于说明 search/fetch/map 调用计数、provider attempts、失败 attempt、warnings 与预算用量。若某个 JSON artifact 不可读，结果会在 `artifact_errors` 里返回稳定错误码，而不是直接让整次读取失败。对 `completed` job，若最终 provenance bundle 缺失核心 sidecar，当前会通过 `artifact_errors` 暴露缺失项；对 `failed` / `canceled` / `interrupted` job，则优先读取当前可解析的 resolved final batch，再退到 checkpoint 与 partial artifacts。`result` 现在也会返回 `watch_attach_after_seq`、`attempt_window_start_seq`、`partial_payload`，并在 `operator_summary` 中镜像 attempt-window / partial-availability 状态，便于 CLI `watch` 与宿主做断点续看。continuation context 在 artifact 可解析但 shape 非法时，也会继续按 `sources.json -> citations.json.source_registry -> checkpoint -> partial/runtime carry-forward` 的顺序回退；当 resolved final batch 缺少 `evidence_items.json` 时，当前会优先回退到当前 job 上的 `evidence_items.json`，而不是直接退到 checkpoint reconstruction。
- `deep_research_artifact(job_id, artifact)` 提供 MCP 单 artifact 读取入口，并沿用 CLI `grok-search-research result --artifact <kind>` 的可见性规则；返回会带 `state` 与 `artifact_visibility_reason`，便于宿主区分当前指针、resolved final batch、checkpoint 或 partial artifact。
- continuation context 当前对 `sources.json`、`citations.json`、`report.json` 逐项判断是否仍可读；某个当前 artifact shape 非法时，不应把仍然可读的其他当前 artifacts 一起降级到 checkpoint。
- `deep_research_resume` 目前支持从 `draft`、`failed`、`interrupted` job 继续，并优先从最新的 completed research-unit checkpoint 续跑，而不是整 job 从头执行。恢复后的新 attempt 会清理上一轮的终态时间戳，并重新使用新的 attempt 时间窗口。
- `deep_research_cancel` 只负责发起取消请求；运行中的 job 会在阶段边界或下一次检查点更新时收口。
- `deep_research_list` 提供最近 job 列表，适合 CLI 或宿主做结果检索。
- 最终 artifacts 当前按同一 `batch_id` 原子发布；调用方如需确认 `sources.json`、`citations.json`、`report.json`、`final_report.md` 来自同一批结果，可读取 artifact metadata 里的 `batch_id`。`sources.json` 当前还可能附带 `topic_match_score` 与 `ranking_penalties`；unused source 若与同域中已被 grounding 的页面相比明显更 off-topic，当前可被直接从最终 source registry 压掉，并带 `same_domain_off_topic` penalty。`report.json.runtime` 当前除 `warnings` 外，也可能附带 `failed_units`。
- `report.json.runtime.warnings` 当前也会在 coverage 明显不完整时附带 `coverage_incomplete`；coverage 现在区分 `coverage_gate_passed` 与 additive `hard_coverage_gate_passed`。`hard_coverage_gate_passed` 只看 `must_cover` / `coverage_checklist` 这类硬目标，决定 release gate 是否直接失败；`unanswered_sections`、`uncovered_sub_questions` 这类软缺口仍会保留在 coverage ledger 中，并至少把 `report.status` 降级为 `degraded`。
- `sources.json` 当前除 `source_id` 外，还会附带 additive `source_key`、`quality_score`、`quality_tier`、`source_type`、`ranking_reasons` 一类来源质量与排序解释元数据；来源条目还可能带 `winner_provider`、`citation_count`、`section_count` 这类更直接的 provenance / usage 字段。`report.json.runtime.provider_winners` 当前会按 unit 暴露 `provider_name`、`provider_model`、`effective_model`、`provider_api_url`、`source_count` 与 `evidence_count`；`provider_attempts` / `provider_capabilities` 会按 search/fetch/map 暴露实际使用或失败的 provider 路径，其中 supplemental search 会带 `attempt_role=supplemental`，search/map 内部的 selective fetch 会带 `attempt_role=selective_fetch`，失败 attempt 还可能带 additive `failure_reason` 与 `warnings`。`report.json.runtime.budget` 会暴露本次 deep research 的预算、并发摘要与本地调用/证据计数；其中 selective fetch 会计入 `budget.usage.fetch_calls`。`citations.json` 与 `report.json` 的 section 当前也可能带 `summary`、`confidence`、`claim_cluster_count`、`supporting_source_count`、`supporting_domain_count`；claim 当前也可能附带 additive `unit_id`、`evidence_ids`、`cluster_type`、`supporting_source_count`、`supporting_domain_count`、`confidence`，用于回溯 claim 来自哪个 research unit / evidence，以及它当前是单来源、同域共识还是更强跨域 corroboration 型证据。
- continuation context 当前会优先复用 `sources.json`；若该 artifact 缺失或不可读，会回退到 `citations.json.source_registry` 重建 carry-forward sources。若已完成 job 的当前 artifact 指针混批或缺件，则会优先回退到最近的完整 final artifact batch，再回退到 checkpoint / partial artifacts。运行中的 `queued` / `running` job 在新进程启动时会被回收成 `interrupted`，并带上恢复原因。
</details>

## 四、常见问题

<details>
<summary>
Q: 必须同时配置 Grok 和 Tavily 吗？
</summary>
A: Grok（`GROK_API_URL` + `GROK_API_KEY`）为必填，提供核心搜索能力。Tavily 和 Firecrawl 均为可选：配置 Tavily 后 `web_fetch` 优先使用 Tavily Extract，失败时降级到 Firecrawl Scrape；两者均未配置时 `web_fetch` 将返回配置错误提示。`web_map` 依赖 Tavily。
</details>

<details>
<summary>
Q: Grok API 地址需要什么格式？
</summary>
A: 推荐使用带显式 `/v1` 后缀的 OpenAI 兼容根路径，并确保 `/chat/completions` 与 `/models` 端点可用；代码层不会仅因省略 `/v1` 就预先拦截，但多数 OpenAI 兼容端点仍可能因此在运行时失败，并通常伴随兼容性 warning。代码层并不要求它必须是“官方”还是“镜像/中转”。
</details>

<details>
<summary>
Q: 如何验证配置？
</summary>
A: 直接调用 `get_config_info` 即可检查基础配置、`/models` 连通性、doctor 状态与 feature readiness；若宿主支持自然语言工具调用，也可让宿主触发同名工具。
</details>

<details>
<summary>
Q: `web_search` 返回空内容或直接报错怎么办？
</summary>
A: 当前版本已经尽量把错误显性化，你可以按以下方式理解：

- `HTTP 503`：上游服务当前不可用，或该模型没有可用通道
- “空的占位 completion 帧（choices=null）”：中转站接受了请求，但没有返回可用正文
- 登录页/认证页相关报错：代理认证失效、被重定向，或上游鉴权异常

建议排查顺序：

1. 先用 `get_config_info` 查看 `doctor` 与 `feature_readiness`，确认 `/models` 和可选依赖探测结果
2. 再切回当前配置或代码默认模型，确认是否为模型兼容性问题
3. 如果仍然不稳定，优先更换中转站，而不是只改默认模型
</details>

## 五、补充文档

- [贡献指南](./CONTRIBUTING.md)
- [安全策略](./SECURITY.md)
- [行为准则](./CODE_OF_CONDUCT.md)
- [兼容性说明](./docs/COMPATIBILITY.md)
- [路线图](./docs/ROADMAP.md)
- [发布说明](./docs/RELEASING.md)
- [更新记录](./CHANGELOG.md)
- [Companion Skill](./skills/research-with-grok-search/SKILL.md)

## 六、Deep Research CLI

```bash
grok-search-research start "Compare open-source deep research frameworks" --watch
grok-search-research list
grok-search-research result JOB_ID --artifact final_report.md
grok-search-research continue JOB_ID "Focus on resume and checkpoint trade-offs" --watch
```

CLI 当前优先承接：
- 持续 watch 事件流
- 读取指定 artifact
- 从已有研究结果和 artifacts 继续开新 job
- 对 draft / interrupted / failed job 按 checkpoint 做 resume

## 许可证

[MIT License](LICENSE)

---

<div align="center">

**如果这个项目对您有帮助，请给个 Star！**

[![Star History Chart](https://api.star-history.com/svg?repos=Boulea7/GrokSearchTool&type=date&legend=top-left)](https://www.star-history.com/#Boulea7/GrokSearchTool&type=date&legend=top-left)
</div>
