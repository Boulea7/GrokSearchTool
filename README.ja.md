[简体中文](README.md) | [繁體中文](README.zh-TW.md) | [English](README.en.md) | 日本語 | [Русский](README.ru.md)

# GrokSearch

GrokSearch は、素早く信頼できるソース付き Web コンテキストを必要とするアシスタントや汎用クライアント向けに独立運用されている MCP サーバーです。

`Grok` の検索能力と `Tavily` / `Firecrawl` の抽出能力を組み合わせ、軽量な検索、ソース確認、対象ページ取得、そして複雑な検索に対する推奨コア経路 `plan_* -> web_search` を支える MCP ツール群を提供します。計画の価値が低い明確な単発検索では、直接 `web_search` を呼ぶこともできます。より重い探索タスクについては、今後 `deep research` 方向へ拡張します。

## 概要

- `web_search`: ソースをキャッシュしながら Web 検索を実行
- `get_sources`: `web_search` のキャッシュ済みソースを取得
- `web_fetch`: Tavily 優先、失敗時は Firecrawl にフォールバック
- `web_map`: サイト構造をマッピング
- `plan_*`: 複雑または曖昧な検索のための段階的プランニング
- `get_config_info`: 設定確認、`/models` 接続性、軽量 doctor
- `switch_model`: デフォルト Grok モデルを切り替え
- `toggle_builtin_tools`: Claude Code の組み込み WebSearch / WebFetch を切り替え

公開 MCP ツールは現在 `13` 個です。

## インストール

### 前提条件

- Python `3.10+`
- `uv`
- stdio MCP をサポートするクライアント

### サポートレベル

- `Officially tested`: Claude Code
- `Community-tested`: Codex スタイルの MCP クライアント、Cherry Studio
- `Planned`: Dify、n8n、Coze

補足:

- 公開ドキュメントが保証するのは現在ローカル `stdio` 構成のみです。
- `toggle_builtin_tools` は Claude Code のプロジェクト設定専用です。
- 以下のインストール例は、現在メンテナンスされている公開配布元 `Boulea7/GrokSearchTool` を使います。

### MCP として追加

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

システム証明書ストアが必要な環境では、`uvx` に `--native-tls` を追加してください。

補足:

- 推奨されるコア経路は `plan_* -> web_search` です。明確な単発検索では直接 `web_search` も利用できます。
- インタラクティブな `deep research` 体験は、MCP / skill ではなく CLI を優先して提供する予定です。
- `web_fetch` は Firecrawl のみでも動作します。
- `web_map` には Tavily と `TAVILY_ENABLED=true` が必要です。
- `web_search` はローカル時間コンテキストを常に注入します。
- `get_config_info` は `connection_test` を維持しつつ、軽量な `doctor` と `feature_readiness` も返します。ただし、完全なエンドツーエンド保証ではありません。

## Companion Skill

このリポジトリには companion skill も含まれています: [`skills/research-with-grok-search`](skills/research-with-grok-search/SKILL.md)

```bash
mkdir -p ~/.codex/skills
ln -s /absolute/path/to/GrokSearch/skills/research-with-grok-search ~/.codex/skills/research-with-grok-search
```

## 開発

```bash
PYTHONPATH=src uv run python -m grok_search.server
uv run --with pytest --with pytest-asyncio pytest -q
uv run --with ruff ruff check .
python3 -m py_compile src/grok_search/*.py src/grok_search/providers/*.py tests/*.py
```

## ドキュメント

- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Compatibility](docs/COMPATIBILITY.md)
- [Roadmap](docs/ROADMAP.md)
- [Changelog](CHANGELOG.md)

## License

[MIT](LICENSE)
