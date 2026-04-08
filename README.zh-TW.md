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

- `Officially tested`：Claude Code（以倉庫內已驗證的本地 `stdio` 路徑與專案級設定路徑為準，並不等同完整宿主 E2E 矩陣）
- `Community-tested`：Codex 風格 MCP 客戶端、Cherry Studio
- `Planned`：Dify、n8n、Coze

說明：

- 公開安裝文件目前只承諾本地 `stdio` 路徑。
- `toggle_builtin_tools` 僅適用於 Claude Code 專案級設定。
- `get_config_info` 中 `toggle_builtin_tools` 的 readiness 只代表偵測到本地 Git 專案上下文，並不等同於完整的 Claude Code host 驗證。
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
    "TAVILY_API_URL": "https://api.tavily.com",
    "FIRECRAWL_API_KEY": "fc-your-firecrawl-key"
  }
}'
```

如需使用系統憑證庫，請在 `uvx` 參數中加入 `--native-tls`。這是 `uvx` 啟動/安裝層的 TLS 排障選項，適合企業代理或自簽憑證鏈；不應將它理解成通用的關閉驗證替代方案。

### 其他 `stdio` 客戶端最小設定

#### Codex CLI / Codex 風格 MCP 客戶端

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

若使用專案級 `.codex/config.toml`，建議不要直接把真實 key 提交到倉庫；本倉庫預設忽略 `.codex/`。本地開發更推薦把敏感變數寫入已忽略的 `.env.local`，再於使用前 `source ./.env.local`。專案級環境變數回退目前同時支援 `KEY=value` 與可選 `export KEY=value`。若會呼叫 `toggle_builtin_tools`，也請避免提交專案級 `.claude/settings.json`；本倉庫同樣預設忽略 `.claude/`。

#### Cherry Studio

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

### 核心環境變數

| 變數 | 必填 | 說明 |
| --- | --- | --- |
| `GROK_API_URL` | 是 | OpenAI 相容 Grok 端點，建議顯式包含 `/v1` |
| `GROK_API_KEY` | 是 | Grok API Key |
| `GROK_MODEL` | 否 | 預設模型；優先級為進程 env > 專案 `.env.local` > 專案 `.env` > 持久化 config > 程式預設 |
| `GROK_TIME_CONTEXT_MODE` | 否 | 時間上下文注入模式：`always` / `auto` / `never` |
| `TAVILY_API_KEY` | 否 | `web_fetch` / `web_map` 用的 Tavily Key，也用於 Tavily supplemental `web_search` |
| `TAVILY_API_URL` | 否 | Tavily API 端點 |
| `TAVILY_ENABLED` | 否 | 是否啟用 Tavily 路徑 |
| `FIRECRAWL_API_KEY` | 否 | Firecrawl fallback Key，也可用於 supplemental `web_search` |
| `FIRECRAWL_API_URL` | 否 | Firecrawl API 端點 |
| `GROK_DEBUG` | 否 | 是否啟用除錯日誌 |
| `GROK_LOG_LEVEL` | 否 | 日誌等級 |
| `GROK_LOG_DIR` | 否 | 日誌目錄；`get_config_info` 會回傳解析後的執行期路徑 |
| `GROK_OUTPUT_CLEANUP` | 否 | 是否啟用 `web_search` 輸出清洗 |
| `GROK_FILTER_THINK_TAGS` | 否 | `GROK_OUTPUT_CLEANUP` 的舊別名 |
| `GROK_RETRY_MAX_ATTEMPTS` | 否 | 最大重試次數 |
| `GROK_RETRY_MULTIPLIER` | 否 | 重試退避倍數 |
| `GROK_RETRY_MAX_WAIT` | 否 | 最大等待秒數 |

補充：

- 模型解析優先級為進程 `GROK_MODEL` 環境變數 > 專案 `.env.local` > 專案 `.env` > `~/.config/grok-search/config.json` 持久化值 > 程式預設值 `grok-4.1-fast`；若使用 OpenRouter 相容位址，執行期會自動補上 `:online` 後綴。
- 環境變數優先權按「鍵是否存在」判定：只要進程環境裡顯式設了某個鍵，即使值是空字串，也不會回落到專案 `.env.local` / `.env`。
- `switch_model` 只會更新 `~/.config/grok-search/config.json` 的持久化層；若同時設了 `GROK_MODEL`，仍以環境變數為準。
- `GROK_TIME_CONTEXT_MODE` 預設為 `always`，保持目前一律注入本地時間上下文的行為。
- 如需節省上下文，可將 `GROK_TIME_CONTEXT_MODE` 設為 `auto`（僅在明顯時效查詢或顯式時效控制下注入）或 `never`。

說明：

- 推薦的核心路徑是 `plan_* -> web_search`；明確單跳查詢可直接使用 `web_search`。
- 互動式 `deep research` 體驗將優先放在 CLI，而不是 MCP / skill 的對話式互動。
- `web_fetch` 在只配置 Firecrawl 時仍可使用。
- `web_map` 需要 Tavily，且 `TAVILY_ENABLED=true`。
- `web_search` 會依 `GROK_TIME_CONTEXT_MODE` 決定是否注入本地時間上下文（預設 `always`）。
- 若上游 endpoint 指向 loopback 位址，該次請求會強制 `trust_env=False`，因此也會一併繞過 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` 與 `SSL_CERT_FILE` / `SSL_CERT_DIR`。
- `get_config_info` 會保留基礎設定快照與 `connection_test`，並由 server 層補充輕量 `doctor`、`feature_readiness` 與最小真實 `search/fetch` 探針；但仍不是完整的端到端保證。
- `web_fetch`、`web_map` 與 Tavily 補充搜尋目前只暴露 provider 能力的受控子集，不等同於 Tavily / Firecrawl 的全量原生參數面。
- `web_fetch` 回傳的是提取後的 Markdown 文字，不會透傳 provider 的完整原始結構化 payload。
- Tavily `web_map` 可能包含外部網域連結；若需要更接近站內 sitemap 的結果，請搭配 `instructions` 收斂並自行過濾。
- `web_fetch` / `web_map` 預設會拒絕非 `http/https`、loopback、明顯私網目標、單標籤主機名、常見私網後綴主機（如 `.internal` / `.local` / `.lan` / `.home` / `.corp`），以及常見把私網 IP 編進公開 DNS 名的 alias 形態（如 `nip.io` / `xip.io` / `sslip.io`）。
- 對通過靜態檢查的目標，`web_fetch` / `web_map` 還會在真正呼叫 provider 前繼續複檢可見的 redirect 目標。
- 這層邊界目前不會只因本機 DNS 將某個看似公開的 hostname 解析到私網就直接拒絕，因此不應被理解成對 split-horizon / 本地 DNS 私有解析的強保證。

### 最小 smoke check

對任何本地 `stdio` host，建議至少做以下驗證：

1. 先呼叫 `get_config_info`，確認基礎設定快照、`connection_test`、`doctor` 與 `feature_readiness` 符合你的安裝目標；未配置對應 provider 時，可選的 `search/fetch` 探針允許跳過
2. 再執行一次 `web_search`
3. 若需要核對來源，再呼叫 `get_sources`
4. 僅在已配置 Tavily / Firecrawl 時驗證 `web_fetch`；僅在已配置並啟用 Tavily 時驗證 `web_map`

補充：

- `doctor.recommendations_detail` 會提供和 `check_id` / feature 關聯的結構化修復建議。
- `feature_readiness.web_fetch.providers` 會附帶 provider 級狀態；`verified_path` 表示真實抓取探針實際打通的後端，未執行的 provider 可能帶有 `skipped_reason`。
- 即使 API Key 已脫敏，診斷結果仍可能包含本機絕對路徑、endpoint/hostname 與精簡後的上游錯誤摘要；對外分享前請先複核。
- `get_sources` 使用目前進程內的記憶體型 LRU 快取（預設 TTL 約 1 小時、上限 256 個 session）；進程重啟、TTL 到期或快取淘汰後，先前的 `session_id` 會失效。
- `get_sources` 回傳的 `rank` 目前會優先保留 Grok 主引用，並保留安全 fragment、剝離 URL `userinfo`、遮罩常見簽名參數。

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
