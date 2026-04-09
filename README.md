![这是图片](./images/title.png)
<div align="center">

<!-- # Grok Search MCP -->

[English](./README.en.md) | [繁體中文](./README.zh-TW.md) | 简体中文 | [日本語](./README.ja.md) | [Русский](./README.ru.md)

**GrokSearch MCP，为 Claude Code 提供轻量、可核验来源的网络上下文能力**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT) [![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/) [![FastMCP](https://img.shields.io/badge/FastMCP-2.3.0+-green.svg)](https://github.com/jlowin/fastmcp)

</div>

---

## 一、概述

GrokSearch MCP 是一个基于 [FastMCP](https://github.com/jlowin/fastmcp) 构建的轻量 MCP 服务器，面向 Claude Code、Codex CLI、Cherry Studio 等支持 MCP 的客户端，提供**最新、可核验、低摩擦**的网络上下文能力。

公开 package import contract 当前分两层：`grok_search.mcp` 是 access-time lazy export，只有真正访问该导出时才需要 `fastmcp`；`grok_search.providers.GrokSearchProvider` 也是 access-time lazy export，普通非 provider 导入不应被其可选依赖连带拖死。这只是在导入时收口边界，不改变安装时依赖声明，也不应被理解为这些依赖已经变成 optional extras。

它不是一个以浏览器自动化为核心的重型系统，而是一层为研究型问答优化的搜索基础设施：

- `Grok` 负责主答案生成
- `Tavily` 负责搜索控制、网页提取与站点映射
- `Firecrawl` 负责抓取托底与补充信源
- `plan_*` 负责复杂问题的轻量规划
- `get_sources` 负责把来源从“答案里的链接”升级成结构化可读取的信源数据

当前推荐主路径是 `plan_* -> web_search`，并在需要来源核对时按需调用 `get_sources`；对明确单跳、低歧义、规划收益很低的查询，也允许直接调用 `web_search`。更重的深度探索能力继续收口到 `deep research`，并优先在 CLI 落地。

当前公开的 `stdio` 安装示例以维护中的发布仓库 `Boulea7/GrokSearchTool` 为准；本地开发工作区、历史远端命名或旧协作痕迹不代表项目仍按 `fork/upstream` PR 模式推进。

```text
Client / Assistant
  └─ MCP / companion skill
      └─ GrokSearch Server
          ├─ plan_*      -> 轻量规划层
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
- `deep research`：更长时间、更强编排的高级研究层，当前优先在 CLI 里承接

### 核心价值

- **答案与来源分离**：`web_search` 返回正文，`get_sources` 返回结构化来源，便于后续核验、排序和复用
- **多 provider 协作**：Grok 负责主回答，Tavily 负责搜索控制 / 抓取 / 映射，Firecrawl 负责托底和补充
- **轻量规划优先**：复杂任务先走 `plan_*`，简单任务直接搜，避免无意义的重编排
- **面向真实运维**：内置 `get_config_info`、feature readiness、最小真实探针、稳定错误契约
- **运行时安全边界**：对抓取/映射目标做 URL 边界收口，对诊断输出和来源 URL 做敏感信息遮罩
- **兼容 OpenAI 风格接入**：可对接 Grok-compatible 中转与镜像站，但实际兼容性仍取决于上游对 `/models` 与 `/chat/completions` 的实现

### 适用场景

- 让代码助手在回答前先查最新文档、API、规范或公告
- 对模型生成内容做来源核验，而不是只看“像不像对”
- 对某个网页做可靠正文抓取，而不是只拿搜索摘要
- 先列清复杂研究任务，再逐步执行
- 在 Claude Code 项目里收口官方 WebSearch/WebFetch 的调用入口


## 二、安装

### 前置条件

- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)（推荐的 Python 包管理器）
- 支持 `stdio` MCP 的客户端，如 Claude Code、Codex CLI、Cherry Studio

### 支持级别

- `Officially tested`：Claude Code（已按仓库内文档验证本地 `stdio` 路径与项目级设置路径，不代表完整宿主 E2E 矩阵）
- `Community-tested`：Codex 风格 MCP 客户端、Cherry Studio
- `Planned`：Dify、n8n、Coze

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
    "GROK_API_URL": "https://your-api-endpoint.com/v1",
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
GROK_API_URL = "https://your-api-endpoint.com/v1"
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
    "GROK_API_URL": "https://your-api-endpoint.com/v1",
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
    "GROK_API_URL": "https://your-api-endpoint.com/v1",
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
| `GROK_MODEL` | 否 | `grok-4.1-fast` | 默认模型；优先级见下方说明（进程 env > 项目 `.env.local` > 项目 `.env` > 持久化 config > 代码默认值） |
| `GROK_TIME_CONTEXT_MODE` | 否 | `always` | 时间上下文注入策略：`always` / `auto` / `never` |
| `TAVILY_API_KEY` | 否 | - | Tavily API 密钥（用于 `web_fetch` / `web_map`，也用于 Tavily supplemental `web_search`） |
| `TAVILY_API_URL` | 否 | `https://api.tavily.com` | Tavily API 地址 |
| `TAVILY_ENABLED` | 否 | `true` | 是否启用 Tavily |
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
| `PYTHONIOENCODING` | 否 | `utf-8` | 建议显式设为 UTF-8，减少 Windows / 中转站日志乱码 |
| `PYTHONUNBUFFERED` | 否 | `1` | 关闭 Python stdout 缓冲，减少 stdio MCP 启动卡顿 |
| `PYTHONUTF8` | 否 | `1` | 强制 Python UTF-8 模式 |

> 模型解析优先级为：进程里的 `GROK_MODEL` > 项目 `.env.local` > 项目 `.env` > `~/.config/grok-search/config.json` 中由 `switch_model` 持久化的值 > 代码默认值 `grok-4.1-fast`。如使用 OpenRouter 兼容地址，运行时还会自动补齐 `:online` 后缀。

> 环境变量优先级按“是否存在”判断：只要进程环境里显式设置了某个键，即使值为空字符串，也不会再回落到项目 `.env.local` / `.env`。

> `GROK_TIME_CONTEXT_MODE` 默认是 `always`，保持当前“全量注入本地时间上下文”的行为；如需节省上下文，可改为 `auto` 或 `never`。

### 本地优先启动建议

如果你本机会频繁改 MCP 代码，建议优先使用本地安装，再将远端仓库作为兜底：

```bash
uv tool install "git+https://github.com/Boulea7/GrokSearchTool.git@main"
```

经验建议：

- `GROK_API_URL` 推荐使用带显式 `/v1` 后缀的 OpenAI 兼容根路径；代码层不会仅因省略 `/v1` 就预先拦截，但多数 OpenAI 兼容端点仍可能因此在运行时失败，并通常伴随兼容性 warning
- `web_search` 调用时若没有用户明确指定模型，尽量不要传 `model` 参数，否则会覆盖默认的 `GROK_MODEL`
- 如需更省上下文，可将 `GROK_TIME_CONTEXT_MODE` 设为 `auto`（只在明显时效查询或显式时效控制下注入）或 `never`
- `GROK_DEBUG=false` 时，`log_info()` 不会写入这类 helper 日志，也不会通过 `ctx.info()` 暴露中间进度；仅在 `GROK_DEBUG=true` 时转发 debug-only progress
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
- `warnings`: 非致命告警列表；例如 Tavily 不可用时，域名过滤和时间范围不会真正作用于补充搜索
- `error`: 稳定的机器可读错误码；无错误时为 `null`

说明：
- `topic`、`time_range`、`include_domains`、`exclude_domains` 当前是 Tavily-backed supplemental search 的能力；如果本次请求没有实际走 Tavily 补充搜索，主请求仍可继续执行，但这些控制项不会真正生效，并会通过 `warnings` 或 `partial` 状态体现。

### `get_sources` — 获取信源

通过 `session_id` 获取对应 `web_search` 的全部信源。

当前 `get_sources` 使用的是当前服务器进程内的内存型 LRU 缓存：默认 TTL 约 1 小时、当前上限 256 个 session。进程重启、TTL 到期或缓存淘汰后，先前的 `session_id` 会失效。

`session_id` 当前只是运行中 server 进程里的 shared-daemon transient handle，不是 durable、caller-bound capability，也不是 secret token；只要在同一个运行中进程里持有该值，就能继续读取对应信源。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | 是 | `web_search` 返回的 `session_id` |

返回值（结构化字典）：
- 成功时返回 `session_id`、`sources_count`、`sources`；其中每个 source 至少包含 `title`、`url`、`provider`、`source_type`、`snippet`、`domain`、`score`、`published_at`、`retrieved_at`、`rank`
- 成功时还会返回 `search_status`、`search_error`、`source_state`，用于区分成功但无信源、部分成功和失败
- miss 时返回 `session_id`、`sources=[]`、`sources_count=0`
- `error`: 仅在 `session_id` 缺失或过期时返回，例如 `session_id_not_found_or_expired`

说明：
- `session_id_not_found_or_expired` 当前统一覆盖进程重启、TTL 到期、LRU 淘汰，以及不可读的旧缓存条目等 miss 场景。
- `sources_count` 当前等于标准化、去重与过滤之后最终写入缓存的信源数量，不等于上游原始 citations 条数。
- `rank` 当前会按 `score`、来源身份清晰度与稳定去重顺序生成，不再对 Grok 引用做额外优先级偏置。
- `standardize_sources` 当前会在去重时规范化 URL 的 scheme/host 大小写，因此同一页面的 mixed-case 变体可能折叠为一个 source；同时会保留普通锚点（fragment），避免不同页面段落引用被误合并，并继续剥离 URL `userinfo`、遮罩常见 query / fragment 签名参数，以及常见 OAuth/OIDC credential 参数（如 `client_secret`、`refresh_token`、`id_token`、`password`）。显式默认端口（如 `:443` / `:80`）当前仍会保留，不会与隐式默认端口 URL 自动折叠。
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
- 若 redirect 预检发生超时或请求级错误，当前实现会把该步骤标记为 `skipped_due_to_error`；`web_fetch` / `web_map` 目前仍会继续执行下游 provider 调用。
- 当前实现为了避免误杀普通公网 hostname，不会因为本机 DNS 把某个公网域名解析到私网结果就直接拒绝请求；因此这层边界不应被理解为对 split-horizon / 本地 DNS 私有解析的强保证。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 目标网页 URL |

### `web_map` — 站点结构映射

通过 Tavily Map API 遍历网站结构，发现 URL 并生成站点地图。

说明：
- Tavily Map 默认可能返回外部域名链接；若你需要更接近站内 sitemap 的结果，请结合 `instructions` 收紧范围，并按返回结果自行过滤。当前文档中的这条说明对应 Tavily 文档中 `allow_external=true` 的默认行为，本封装暂未直接暴露该开关。
- 默认会拒绝非 `http/https`、loopback、明显私有网络目标、单标签主机名、常见私网后缀主机、常见 loopback helper 域名（如 `localtest.me` / `lvh.me`），以及常见把私网 IP 编进公网 DNS 名的 alias 形态，并在调用 Tavily 前继续做可见 redirect 目标复检。
- 可见 redirect 复检当前使用 `GET` 而不是 `HEAD`；若预检超时或发生请求级错误，会标记为 `skipped_due_to_error`，并继续执行下游 provider。该边界不应被理解为对 split-horizon / 本地 DNS 私有解析的强保证。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `url` | string | 是 | - | 起始 URL |
| `instructions` | string | 否 | `""` | 自然语言过滤指令 |
| `max_depth` | int | 否 | `1` | 最大遍历深度（1-5） |
| `max_breadth` | int | 否 | `20` | 每页最大跟踪链接数（1-500） |
| `limit` | int | 否 | `50` | 总链接处理数上限（1-500） |
| `timeout` | int | 否 | `150` | 超时秒数（10-150） |

### `get_config_info` — 配置诊断

支持可选参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `detail` | string | 否 | `"full"` | 返回级别：`"full"` 保留完整 doctor/probe 细节，`"summary"` 仅保留高频机器可读摘要 |

`Config.get_config_info()` 只负责返回基础配置快照；MCP 工具 `get_config_info` 会保留该快照，并由 server 层补充诊断结果，返回：

- Grok `/models` 连通性与可用模型
- Tavily / Firecrawl 的只读探测结果（仅在已配置时执行）
- 默认最小真实 `web_search` / `web_fetch` 探针结果
- `web_search` / `get_sources` / `web_fetch` / `web_map` / `toggle_builtin_tools` 的 readiness 汇总
- 修复建议列表（API Key 自动脱敏）
- `doctor.recommendations_detail`：与 `check_id` / `feature` 关联的结构化修复建议
- `feature_readiness.web_fetch.providers`：provider 级状态，`verified_path` 表示真实抓取探针实际打通的后端；未执行的 provider 可能附带 `skipped_reason`

注意：
- `detail="full"` 保留完整 `doctor.checks`、`doctor.recommendations_detail` 和 provider/probe 细节；`detail="summary"` 只保留基础配置快照、`connection_test`、`doctor.status/summary/recommendations` 与 `feature_readiness`
- `detail="summary"` 当前只是同一次诊断结果的紧凑字段投影，不是额外的“轻执行路径”；底层仍会执行同一轮配置/探针逻辑。
- `feature_readiness.get_sources` 只有在当前进程内至少存在一个非 error 的可读取 source session 时才会显示 `ready`；如果只有失败搜索留下的 session，状态会保持 `partial_ready`
- `feature_readiness` / `doctor` 的状态语义当前可按以下方式理解：`ready`=当前能力已验证可用，`degraded`=能力存在但探针或局部依赖异常，`not_ready`=配置或前置条件不足，`partial_ready`=接口存在但仍缺少运行中瞬时条件；其中 `transient` 和 `client_specific` 项默认不拉低 overall doctor。
- 输出中的 API Key 会脱敏；显而易见的 bearer/token/签名 query，以及常见 OAuth/OIDC credential 参数也会做遮罩。但诊断结果仍可能包含本机绝对路径、endpoint/主机名或精简后的上游错误摘要；若要贴到 issue / 聊天，请先二次检查并按需删减。

### `switch_model` — 模型切换

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | 是 | 模型 ID（如 `"grok-4-fast"`, `"grok-2-latest"`） |

切换后配置持久化到 `~/.config/grok-search/config.json`，跨会话保持。
若当前进程或项目 `.env.local` / `.env` 已显式设置 `GROK_MODEL`，`switch_model` 仍会写入持久化配置，但当前进程的实际生效模型不会立刻改变。

### `toggle_builtin_tools` — 工具路由控制

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `action` | string | 否 | `"status"` | `"on"` 禁用官方工具 / `"off"` 启用官方工具 / `"status"` 查看状态 |

通过修改项目级 `.claude/settings.json` 的 `permissions.deny`，为 Claude Code 添加或移除内建网页工具的 deny 规则。

### `plan_intent` / `plan_complexity` / `plan_sub_query` / `plan_search_term` / `plan_tool_mapping` / `plan_execution`

结构化搜索规划脚手架（分阶段、多轮），用于在执行复杂搜索前先生成可执行的搜索计划。
其中 `plan_search_term` 在首次建立 `search_strategy` 时设置 `approach` / `fallback_plan`；后续非 `is_revision` 调用只会追加 `search_terms`，不会隐式改写既有 strategy metadata。
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
A: 在 Claude 对话中说"显示 grok-search 配置信息"，将自动测试 API 连接并显示结果。
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
- [更新记录](./CHANGELOG.md)
- [Companion Skill](./skills/research-with-grok-search/SKILL.md)

## 许可证

[MIT License](LICENSE)

---

<div align="center">

**如果这个项目对您有帮助，请给个 Star！**

[![Star History Chart](https://api.star-history.com/svg?repos=Boulea7/GrokSearchTool&type=date&legend=top-left)](https://www.star-history.com/#Boulea7/GrokSearchTool&type=date&legend=top-left)
</div>
