[簡體中文](README.md) | 繁體中文 | [English](README.en.md) | [日本語](README.ja.md) | [Русский](README.ru.md)

# GrokSearch

GrokSearch 是一個獨立維護的 MCP 伺服器，面向需要快速、可靠、可核驗來源的網頁上下文能力的助理與通用客戶端。

它整合 `Grok` 搜尋與 `Tavily`、`Firecrawl` 擷取能力，提供適合輕量查詢、來源核對、聚焦抓取，並以 `plan_* -> web_search` 為推薦核心路徑的 MCP 工具面；對於明確單跳、低歧義且規劃收益很低的查詢，也允許直接呼叫 `web_search`；對更重的探索任務，未來將以 `deep research` 方向擴展。

## 功能概覽

- `web_search`：AI 驅動的網頁搜尋並快取信源
- `get_sources`：讀取 `web_search` 快取的信源
- `web_fetch`：優先 Tavily，失敗時回退 Firecrawl
- `web_map`：網站結構映射
- `plan_*`：複雜或含糊搜尋的分階段規劃工具
- `get_config_info`：檢查設定、`/models` 連通性與輕量 doctor
- `switch_model`：切換預設 Grok 模型
- `toggle_builtin_tools`：切換 Claude Code 內建 WebSearch / WebFetch

目前公開 MCP 工具共 `13` 個：

- `web_search`
- `get_sources`
- `web_fetch`
- `web_map`
- `get_config_info`
- `switch_model`
- `toggle_builtin_tools`
- `plan_intent`
- `plan_complexity`
- `plan_sub_query`
- `plan_search_term`
- `plan_tool_mapping`
- `plan_execution`

## 安裝

### 前置條件

- Python `3.10+`
- `uv`
- 支援 stdio MCP 的客戶端，例如 Claude Code、Codex CLI、Cherry Studio

### 支援等級

- `Officially tested`：Claude Code
- `Community-tested`：Codex 風格 MCP 客戶端、Cherry Studio
- `Planned`：Dify、n8n、Coze

說明：

- 公開安裝文件目前只承諾本地 `stdio` 路徑。
- `toggle_builtin_tools` 僅適用於 Claude Code 專案級設定。
- 下方安裝片段預設使用目前維護中的公開來源 `Boulea7/GrokSearchTool`。

### 安裝為 MCP

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
    "FIRECRAWL_API_KEY": "fc-your-firecrawl-key"
  }
}'
```

如需使用系統憑證庫，請在 `uvx` 參數中加入 `--native-tls`。

說明：

- 推薦的核心路徑是 `plan_* -> web_search`；明確單跳查詢可直接使用 `web_search`。
- 互動式 `deep research` 體驗將優先放在 CLI，而不是 MCP / skill 的對話式互動。
- `web_fetch` 在只配置 Firecrawl 時仍可使用。
- `web_map` 需要 Tavily，且 `TAVILY_ENABLED=true`。
- `web_search` 會注入本地時間上下文。
- `get_config_info` 會保留 `connection_test`，並提供輕量 `doctor` 與 `feature_readiness` 視圖，但仍不是完整的端到端保證。

## Companion Skill

本倉庫同時提供 companion skill：[`skills/research-with-grok-search`](skills/research-with-grok-search/SKILL.md)

```bash
mkdir -p ~/.codex/skills
ln -s /absolute/path/to/GrokSearch/skills/research-with-grok-search ~/.codex/skills/research-with-grok-search
```

## 開發

```bash
PYTHONPATH=src uv run python -m grok_search.server
uv run --with pytest --with pytest-asyncio pytest -q
uv run --with ruff ruff check .
python3 -m py_compile src/grok_search/*.py src/grok_search/providers/*.py tests/*.py
```

## 倉庫文件

- [貢獻指南](CONTRIBUTING.md)
- [安全策略](SECURITY.md)
- [行為準則](CODE_OF_CONDUCT.md)
- [相容性說明](docs/COMPATIBILITY.md)
- [路線圖](docs/ROADMAP.md)
- [更新記錄](CHANGELOG.md)

## License

[MIT](LICENSE)
