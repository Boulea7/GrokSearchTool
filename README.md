![这是图片](./images/title.png)
<div align="center">

<!-- # Grok Search MCP -->

[English](./README.en.md) | [繁體中文](./README.zh-TW.md) | 简体中文 | [日本語](./README.ja.md) | [Русский](./README.ru.md)

**GrokSearch MCP，为 Claude Code 提供轻量、可核验来源的网络上下文能力**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT) [![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/) [![FastMCP](https://img.shields.io/badge/FastMCP-2.0.0+-green.svg)](https://github.com/jlowin/fastmcp)

</div>

---

## 一、概述

Grok Search MCP 是一个基于 [FastMCP](https://github.com/jlowin/fastmcp) 构建的轻量 MCP 服务器，采用**双引擎架构**：**Grok** 负责 AI 驱动的智能搜索，**Tavily** 负责高保真网页抓取与站点映射，各取所长为 Claude Code / Cherry Studio / Codex CLI 等 LLM Client 提供可核验来源的实时网络上下文能力。当前推荐主路径是 `plan_* -> web_search`；对于明确单跳、低歧义、规划收益很低的查询，也允许直接调用 `web_search`。更重的深度探索能力将继续收口到 `deep research`，并优先在 CLI 落地。

当前公开的 `stdio` 安装示例以维护中的发布仓库 `Boulea7/GrokSearchTool` 为准；本地开发工作区、历史远端命名或旧协作痕迹不代表项目仍按 `fork/upstream` PR 模式推进。

```
Claude ──MCP──► Grok Search Server
                  ├─ web_search  ───► Grok API（AI 搜索）
                  ├─ web_fetch   ───► Tavily Extract → Firecrawl Scrape（内容抓取，自动降级）
                  └─ web_map     ───► Tavily Map（站点映射）
```

### 功能特性

- **双引擎**：Grok 搜索 + Tavily 抓取/映射，互补协作
- **Firecrawl 托底**：Tavily 提取失败时自动降级到 Firecrawl Scrape，支持空内容自动重试
- **OpenAI 兼容接口**，支持任意 Grok 镜像站
- **自动时间注入**（默认注入本地时间上下文）
- **推荐核心路径**：默认先 `plan_*` 再 `web_search`；对明确单跳查询仍允许直接 `web_search`
- 一键禁用 Claude Code 官方 WebSearch/WebFetch，强制路由到本工具
- 智能重试（支持 Retry-After 头解析 + 指数退避）
- 父进程监控（Windows 下自动检测父进程退出，防止僵尸进程）
- **未来高级能力方向**：更重的深度探索能力将以 `deep research` 框架推进，交互式体验优先在 CLI 落地

### 效果展示
我们以在`cherry studio`中配置本MCP为例，展示了`claude-opus-4.6`模型如何通过本项目实现外部知识搜集，降低幻觉率。
![](./images/wogrok.png)
如上图，**为公平实验，我们打开了claude模型内置的搜索工具**，然而opus 4.6仍然相信自己的内部常识，不查询FastAPI的官方文档，以获取最新示例。
![](./images/wgrok.png)
如上图，当打开`grok-search MCP`时，在相同的实验条件下，opus 4.6主动调用多次搜索，以**获取官方文档，回答更可靠。** 


## 二、安装

### 前置条件

- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)（推荐的 Python 包管理器）
- 支持 `stdio` MCP 的客户端，如 Claude Code、Codex CLI、Cherry Studio

### 支持级别

- `Officially tested`：Claude Code
- `Community-tested`：Codex 风格 MCP 客户端、Cherry Studio
- `Planned`：Dify、n8n、Coze

说明：

- 公开安装文档当前只承诺本地 `stdio` 路径
- `toggle_builtin_tools` 仅适用于 Claude Code 项目级设置
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
FIRECRAWL_API_KEY = "fc-your-firecrawl-key"
```

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
| `GROK_API_URL` | 是 | - | Grok API 地址（OpenAI 兼容格式） |
| `GROK_API_KEY` | 是 | - | Grok API 密钥 |
| `GROK_MODEL` | 否 | `grok-4.1-fast` | 默认模型（设置后优先于 `~/.config/grok-search/config.json`） |
| `GROK_TIME_CONTEXT_MODE` | 否 | `always` | 时间上下文注入策略：`always` / `auto` / `never` |
| `TAVILY_API_KEY` | 否 | - | Tavily API 密钥（用于 web_fetch / web_map） |
| `TAVILY_API_URL` | 否 | `https://api.tavily.com` | Tavily API 地址 |
| `TAVILY_ENABLED` | 否 | `true` | 是否启用 Tavily |
| `FIRECRAWL_API_KEY` | 否 | - | Firecrawl API 密钥（Tavily 失败时托底） |
| `FIRECRAWL_API_URL` | 否 | `https://api.firecrawl.dev/v2` | Firecrawl API 地址 |
| `GROK_DEBUG` | 否 | `false` | 调试模式 |
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

> 模型解析优先级为：`GROK_MODEL` 环境变量 > `~/.config/grok-search/config.json` 中由 `switch_model` 持久化的值 > 代码默认值 `grok-4.1-fast`。如使用 OpenRouter 兼容地址，运行时还会自动补齐 `:online` 后缀。

> `GROK_TIME_CONTEXT_MODE` 默认是 `always`，保持当前“全量注入本地时间上下文”的行为；如需节省上下文，可改为 `auto` 或 `never`。

### 本地优先启动建议

如果你本机会频繁改 MCP 代码，建议优先使用本地安装，再将远端仓库作为兜底：

```bash
uv tool install "git+https://github.com/Boulea7/GrokSearchTool.git@main"
```

经验建议：

- `GROK_API_URL` 尽量写成 OpenAI 兼容根路径并显式带上 `/v1`
- `web_search` 调用时若没有用户明确指定模型，尽量不要传 `model` 参数，否则会覆盖默认的 `GROK_MODEL`
- 如需更省上下文，可将 `GROK_TIME_CONTEXT_MODE` 设为 `auto`（只在明显时效查询或显式时效控制下注入）或 `never`
- 若 `content` 为空，先检查中转站是否真的返回了正文；若 `sources_count=0`，再检查是否提供了结构化 citations，或正文里是否至少包含可解析的 Markdown 链接 / 裸 URL


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

显示连接成功后，我们**十分推荐**在 Claude 对话中输入
```
调用 grok-search toggle_builtin_tools，关闭Claude Code's built-in WebSearch and WebFetch tools
```
工具将自动修改**项目级** `.claude/settings.json` 的 `permissions.deny`，一键禁用 Claude Code 官方的 WebSearch 和 WebFetch，从而迫使claude code调用本项目实现搜索！



## 三、MCP 工具介绍

<details>
<summary>本项目提供 MCP 工具（展开查看）</summary>

### `web_search` — AI 网络搜索

通过 Grok API 执行 AI 驱动的网络搜索，默认仅返回 Grok 的回答正文，并返回 `session_id` 以便后续获取信源。

`web_search` 输出不展开完整信源，仅返回 `sources_count` 与结构化状态字段；信源会按 `session_id` 缓存在服务端，可用 `get_sources` 拉取。

默认推荐对非显而易见的单跳任务先走 `plan_* -> web_search`；如果查询本身已经足够明确、低歧义，且 planning 只会增加摩擦，则可直接调用 `web_search`。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | 是 | - | 搜索查询语句 |
| `platform` | string | 否 | `""` | 聚焦平台（如 `"Twitter"`, `"GitHub, Reddit"`） |
| `model` | string | 否 | `null` | 按次指定 Grok 模型 ID |
| `extra_sources` | int | 否 | `0` | 额外补充信源数量（Tavily/Firecrawl，可为 0 关闭） |
| `topic` | string | 否 | `"general"` | 补充搜索主题，目前支持 `general` / `news` |
| `time_range` | string | 否 | `null` | 相对时间范围，目前支持 `day` / `week` / `month` / `year` |
| `include_domains` | string[] | 否 | `[]` | Tavily 补充搜索白名单域名 |
| `exclude_domains` | string[] | 否 | `[]` | Tavily 补充搜索黑名单域名 |

默认会注入本地时间上下文，以提升时效性搜索的准确度；可通过 `GROK_TIME_CONTEXT_MODE=always|auto|never` 调整。

返回值（结构化字典）：
- `session_id`: 本次查询的会话 ID
- `content`: Grok 回答正文（已自动剥离信源）
- `sources_count`: 已缓存的信源数量
- `status`: `ok` / `partial` / `error`
- `effective_params`: 最终生效的搜索参数回显
- `warnings`: 非致命告警列表；例如 Tavily 不可用时，域名过滤和时间范围不会真正作用于补充搜索
- `error`: 稳定的机器可读错误码；无错误时为 `null`

### `get_sources` — 获取信源

通过 `session_id` 获取对应 `web_search` 的全部信源。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | 是 | `web_search` 返回的 `session_id` |

返回值（结构化字典）：
- `session_id`
- `sources_count`
- `sources`: 信源列表；每项至少包含 `title`、`url`、`provider`、`source_type`、`snippet`、`domain`、`score`、`published_at`、`retrieved_at`、`rank`

### `web_fetch` — 网页内容抓取

通过 Tavily Extract API 获取完整网页内容，返回 Markdown 格式。Tavily 失败时自动降级到 Firecrawl Scrape 进行托底抓取。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 目标网页 URL |

### `web_map` — 站点结构映射

通过 Tavily Map API 遍历网站结构，发现 URL 并生成站点地图。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `url` | string | 是 | - | 起始 URL |
| `instructions` | string | 否 | `""` | 自然语言过滤指令 |
| `max_depth` | int | 否 | `1` | 最大遍历深度（1-5） |
| `max_breadth` | int | 否 | `20` | 每页最大跟踪链接数（1-500） |
| `limit` | int | 否 | `50` | 总链接处理数上限（1-500） |
| `timeout` | int | 否 | `150` | 超时秒数（10-150） |

### `get_config_info` — 配置诊断

无需参数。`Config.get_config_info()` 只负责返回基础配置快照；MCP 工具 `get_config_info` 会保留该快照，并由 server 层补充诊断结果，返回：

- Grok `/models` 连通性与可用模型
- Tavily / Firecrawl 的只读探测结果（仅在已配置时执行）
- 默认最小真实 `web_search` / `web_fetch` 探针结果
- `web_search` / `get_sources` / `web_fetch` / `web_map` / `toggle_builtin_tools` 的 readiness 汇总
- 修复建议列表（API Key 自动脱敏）

### `switch_model` — 模型切换

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | 是 | 模型 ID（如 `"grok-4-fast"`, `"grok-2-latest"`） |

切换后配置持久化到 `~/.config/grok-search/config.json`，跨会话保持。

### `toggle_builtin_tools` — 工具路由控制

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `action` | string | 否 | `"status"` | `"on"` 禁用官方工具 / `"off"` 启用官方工具 / `"status"` 查看状态 |

修改项目级 `.claude/settings.json` 的 `permissions.deny`，一键禁用 Claude Code 官方的 WebSearch 和 WebFetch。

### `plan_intent` / `plan_complexity` / `plan_sub_query` / `plan_search_term` / `plan_tool_mapping` / `plan_execution`

结构化搜索规划脚手架（分阶段、多轮），用于在执行复杂搜索前先生成可执行的搜索计划。
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
A: 需要 OpenAI 兼容格式的 API 地址（支持 `/chat/completions` 和 `/models` 端点）。如使用官方 Grok，需通过兼容 OpenAI 格式的镜像站访问。
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
