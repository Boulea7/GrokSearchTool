# GrokSearch

## 功能描述

GrokSearch 是一个基于 FastMCP 的 MCP 服务器，提供以下能力：

- `web_search`：通过 Grok 执行 AI 驱动的网络搜索
- `get_sources`：读取 `web_search` 缓存的信源
- `web_fetch`：通过 Tavily 提取网页内容，并在失败时回退到 Firecrawl
- `web_map`：通过 Tavily 生成站点地图
- `plan_*`：为复杂搜索提供分阶段结构化规划
- `get_config_info` / `switch_model` / `toggle_builtin_tools`：配置诊断、模型切换、Claude Code 工具路由控制

## 使用方法

### 本地开发

```bash
PYTHONPATH=src uv run python -m grok_search.server
```

### 测试

```bash
uv run --with pytest --with pytest-asyncio pytest -q
```

### 安装为 MCP

请参考 [README.md](./README.md) 与 [docs/README_EN.md](./docs/README_EN.md) 中的 `claude mcp add-json` 示例。

## 参数说明

### 核心环境变量

- `GROK_API_URL`：Grok/OpenAI-compatible API 地址
- `GROK_API_KEY`：Grok API 密钥
- `GROK_MODEL`：默认模型，未设置时使用代码默认值
- `TAVILY_API_KEY` / `TAVILY_API_URL`：Tavily 配置
- `FIRECRAWL_API_KEY` / `FIRECRAWL_API_URL`：Firecrawl 配置
- `GROK_RETRY_MAX_ATTEMPTS` / `GROK_RETRY_MULTIPLIER` / `GROK_RETRY_MAX_WAIT`：重试配置
- `GROK_OUTPUT_CLEANUP`：是否启用 `web_search` 输出清洗

## 返回值说明

### `web_search`

返回 `dict`：

- `session_id`：信源缓存 ID
- `content`：搜索结果正文，失败时返回可诊断错误文本
- `sources_count`：缓存的信源数量

### `get_sources`

返回 `dict`：

- `session_id`
- `sources`
- `sources_count`

## 项目规划

### 当前状态

- 已补强 `plan_*` 阶段顺序与缺失会话错误提示
- 已补强 `web_search` 输出清洗
- 已将 Grok provider 调整为非流式 completion 优先，并保留 SSE 文本兼容
- 已补强本机 `grok-search-mcp` 包装脚本为“优先本地、失败后回退 fork 远端”，当前远端默认指向 `Boulea7/GrokSearchTool@main`
- 已补充 Windows `SelectorEventLoopPolicy` 兼容逻辑
- 已增强 `web_search` 对上游 `503`、空占位 completion 帧与不可解析响应的错误显性化，并尽量保留 `request_id`
- 已增强 `web_search` 对 OpenAI 兼容 `message.content` 数组块、结构化 citations/annotations、正文内联链接的解析与兜底，减少 `content` 为空及 `sources_count=0` 的情况
- 已增强 `web_fetch` 对 Tavily / Firecrawl 的失败归因，能区分重定向、认证失败与空内容
- 已在本机共享环境文件 `~/.config/grok-search/grok-search.env` 中将主用 Grok 中转站切换为 `https://ai.huan666.de/v1`，并保留原站 `https://api.925214.xyz/v1` 作为备用配置

### 待办

- 继续评估 `main` 与 `grok-with-tavily` 分支的安装入口与默认模型漂移
- 视上游维护情况决定是否进一步统一英文文档与安装指引
- 如后续需要，再单独为 `grok-with-tavily` 线整理兼容性补丁
- 继续观察不同 Grok 中转站对 `/v1/chat/completions` 的兼容质量，必要时再讨论是否引入更受限的协议 fallback
- 评估是否在 `get_config_info` 中直接提示 `GROK_API_URL` 缺少 `/v1` 的常见误配
- 继续观察是否有上游会把 citations 放在更深层的自定义字段中，必要时再扩展结构化信源提取白名单
