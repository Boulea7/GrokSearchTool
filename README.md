![这是图片](./images/title.png)
<div align="center">

<!-- # Grok Search MCP -->

[English](./docs/README_EN.md) | 简体中文

**通过 MCP 协议将 Grok 搜索能力集成到 Claude，显著增强文档检索与事实核查能力**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastMCP](https://img.shields.io/badge/FastMCP-2.0.0+-green.svg)](https://github.com/jlowin/fastmcp)

</div>

---

## 概述

Grok Search MCP 是一个基于 [FastMCP](https://github.com/jlowin/fastmcp) 构建的 MCP（Model Context Protocol）服务器，通过转接第三方平台（如 Grok）的强大搜索能力，为 Claude、Claude Code 等 AI 模型提供实时网络搜索功能。

### 核心价值

- **突破知识截止限制**：让 Claude 访问最新的网络信息，不再受训练数据时间限制
- **增强事实核查**：实时搜索验证信息的准确性和时效性
- **结构化输出**：返回包含标题、链接、摘要的标准化 JSON，便于 AI 模型理解与引用
- **即插即用**：通过 MCP 协议无缝集成到 Claude Desktop、Claude Code 等客户端

### 工作原理

```
Claude/Claude Code → MCP 调用 → Search MCP 服务 → 转接 Grok API → 网络搜索/内容抓取 → 结构化结果返回
```

**`web_search` 搜索流程**：
1. Claude 通过 MCP 协议调用 `web_search` 工具
2. Search MCP 将请求转发到 Grok 第三方平台（OpenAI 兼容接口）
3. Grok 执行实时网络搜索并返回结果
4. Search MCP 格式化为结构化 JSON：`{title, url, content}`
5. Claude 基于搜索结果生成更准确、更新的回答

**`web_fetch` 抓取流程**：
1. Claude 通过 MCP 协议调用 `web_fetch` 工具
2. Search MCP 将 URL 发送到 Grok API
3. Grok 获取网页内容并解析 HTML 结构
4. 返回包含元数据、目录、正文的结构化 Markdown 文档
5. Claude 基于完整网页内容进行分析和回答

## 为什么选择 Grok？

与其他搜索方案对比：

| 特性 | Grok Search MCP | Google Custom Search API | Bing Search API | SerpAPI |
|------|----------------|-------------------------|-----------------|---------|
| **AI 优化结果** | ✅ 专为 AI 理解优化 | ❌ 通用搜索结果 | ❌ 通用搜索结果 | ❌ 通用搜索结果 |
| **内容摘要质量** | ✅ AI 生成高质量摘要 | ⚠️ 需二次处理 | ⚠️ 需二次处理 | ⚠️ 需二次处理 |
| **实时性** | ✅ 实时网络数据 | ✅ 实时 | ✅ 实时 | ✅ 实时 |
| **集成复杂度** | ✅ MCP 即插即用 | ⚠️ 需自行开发 | ⚠️ 需自行开发 | ⚠️ 需自行开发 |
| **返回格式** | ✅ AI 友好 JSON | ⚠️ 需格式化 | ⚠️ 需格式化 | ⚠️ 需格式化 |

## 功能特性

- ✅ 通过 OpenAI 兼容格式调用 Grok 搜索能力
- ✅ 环境变量配置，安全便捷
- ✅ 格式化搜索结果输出（标题 + 链接 + 摘要）
- ✅ 可扩展架构，支持添加其他搜索 Provider
- ✅ 完善的日志系统，便于调试和监控
- ✅ 调试模式开关，方便开发测试
- ✅ 支持指定聚焦的搜索平台（如 Twitter、Reddit、GitHub 等）
- ✅ 可配置搜索结果数量范围
- ✅ 网页内容抓取：自动获取 URL 内容并转换为结构化 Markdown

## 快速开始

<details>
<summary><h3>0. 前置要求（点击展开）</h3></summary>

#### 安装必要工具

**Python 环境**：
- Python 3.10 或更高版本
- 已配置 Claude Code 或 Claude Desktop

**uv 工具**（推荐的 Python 包管理器）：

请确保您已成功安装 [uv 工具](https://docs.astral.sh/uv/getting-started/installation/)：

<details>
<summary><b>Windows 安装</b></summary>

在 PowerShell 中运行以下命令：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

</details>

<details>
<summary><b>Linux/macOS 安装</b></summary>

使用 curl 或 wget 下载并安装：

```bash
# 使用 curl
curl -LsSf https://astral.sh/uv/install.sh | sh

# 或使用 wget
wget -qO- https://astral.sh/uv/install.sh | sh
```

</details>

> **💡 重要提示**：我们**强烈推荐** Windows 用户在 WSL（Windows Subsystem for Linux）中运行本项目！

#### 获取 Grok API 访问权限

- 注册支持 Grok 的第三方平台账户
- 获取 API Endpoint 和 API Key


</details>

### 1. 安装与配置

使用 `claude mcp add-json` 一键安装并配置：

```bash
claude mcp add-json grok-search --scope user '{
  "type": "stdio",
  "command": "uvx",
  "args": [
    "--from",
    "git+https://github.com/your-org/GrokSearch.git",
    "grok-search"
  ],
  "env": {
    "GROK_API_URL": "https://api.example.com/v1",
    "GROK_API_KEY": "your-api-key-here"
  }
}'
```

#### 配置说明

配置通过**环境变量**进行，安装时直接在 `env` 字段中设置：

| 环境变量 | 必填 | 默认值 | 说明 |
|---------|------|--------|------|
| `GROK_API_URL` | ✅ | - | Grok API 地址（支持 OpenAI 格式） |
| `GROK_API_KEY` | ✅ | - | 您的 API Key |
| `GROK_DEBUG` | ❌ | `false` | 调试模式开关（`true`/`false`） |
| `GROK_LOG_LEVEL` | ❌ | `INFO` | 日志级别（DEBUG/INFO/WARNING/ERROR） |
| `GROK_LOG_DIR` | ❌ | `logs` | 日志文件存储目录 |

### 2. 验证安装

```bash
claude mcp list
```

应能看到 `grok-search` 服务器已注册。

### 3. 项目相关说明

#### MCP 工具说明

本项目提供两个 MCP 工具：

##### `web_search` - 网络搜索

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | ✅ | - | 搜索查询语句 |
| `platform` | string | ❌ | `""` | 聚焦搜索平台（如 `"Twitter"`, `"GitHub, Reddit"`） |
| `min_results` | int | ❌ | `3` | 最少返回结果数 |
| `max_results` | int | ❌ | `10` | 最多返回结果数 |

返回 JSON 结构：

```json
{
  "results": [
    {
      "title": "FastMCP - Python framework for MCP servers",
      "url": "https://github.com/jlowin/fastmcp",
      "content": "FastMCP is a Python framework..."
    }
  ],
  "provider": "grok",
  "query": "FastMCP latest version"
}
```

##### `web_fetch` - 网页内容抓取

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | ✅ | 目标网页 URL（HTTP/HTTPS） |

功能特点：
- 获取完整网页 HTML 内容并解析
- 自动转换为结构化 Markdown 格式
- 保留原网页的标题层级、列表、表格、代码块等元素
- 输出包含元数据头部（来源 URL、标题、抓取时间）
- 移除脚本、样式等非内容元素

返回 Markdown 结构示例：

```markdown
---
source: [原始URL]
title: [网页标题]
fetched_at: [抓取时间]
---

## 目录
- [章节1](#章节1)
- [章节2](#章节2)

## 章节1
网页正文内容...
```

---

## 项目架构

```
grok-search/
├── pyproject.toml              # 项目元数据与依赖
├── README.md                   # 项目文档
└── src/grok_search/
    ├── __init__.py             # 包入口
    ├── config.py               # 配置管理（环境变量加载）
    ├── logger.py               # 日志系统
    ├── server.py               # MCP 服务器主程序
    ├── utils.py                # 结果格式化工具
    └── providers/              # 搜索 Provider 抽象层
        ├── __init__.py
        ├── base.py             # SearchProvider 基类
        └── grok.py             # Grok API 实现
```

### 核心模块说明

| 模块 | 职责 |
|------|------|
| `server.py` | FastMCP 服务入口，注册 `web_search` 和 `web_fetch` 工具 |
| `config.py` | 单例模式管理环境变量配置 |
| `providers/base.py` | 定义 `SearchProvider` 抽象接口和 `SearchResult` 数据模型 |
| `providers/grok.py` | 实现 Grok API 调用（搜索和内容抓取） |
| `utils.py` | 格式化工具和 Prompt 模板管理 |

## Others

## 常见问题

<details>
<summary><b>Q: 如何获取 Grok API 访问权限？</b></summary>

A: 本项目使用第三方平台转接 Grok API。您需要：
1. 注册支持 Grok 的第三方服务
2. 获取 API Endpoint 和 API Key
3. 使用 `claude mcp add-json` 命令配置环境变量（参见安装配置章节）
</details>


<details>
<summary><b>Q: 搜索结果数量如何控制？</b></summary>

A: `web_search` 工具接受以下参数控制搜索结果：
- `min_results`：最少返回结果数（默认 3）
- `max_results`：最多返回结果数（默认 10）

Claude 会根据需要自动调整这些参数。
</details>

<details>
<summary><b>Q: 如何指定搜索特定平台？</b></summary>

A: `web_search` 工具支持 `platform` 参数，可以指定搜索聚焦的平台。例如：
- `platform="Twitter"` - 聚焦 Twitter 搜索结果
- `platform="GitHub, Reddit"` - 聚焦 GitHub 和 Reddit 的结果
- 留空则搜索全网
</details>


## 许可证

本项目采用 [MIT License](LICENSE) 开源。

---

<div align="center">

**如果这个项目对您有帮助，请给个 ⭐ Star！**
[![Star History Chart](https://api.star-history.com/svg?repos=GuDaStudio/GrokSearch&type=date&legend=top-left)](https://www.star-history.com/#GuDaStudio/GrokSearch&type=date&legend=top-left)
</div>
