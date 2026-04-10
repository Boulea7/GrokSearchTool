[简体中文](README.md) | [繁體中文](README.zh-TW.md) | [English](README.en.md) | 日本語 | [Русский](README.ru.md)

# GrokSearch

GrokSearch は、素早く信頼できるソース付き Web コンテキストを必要とするアシスタントや汎用クライアント向けに独立運用されている MCP サーバーです。

`Grok` の検索能力と `Tavily` / `Firecrawl` の抽出能力を組み合わせ、軽量な検索、ソース確認、対象ページ取得、そして複雑な検索に対する推奨コア経路 `plan_* -> web_search` を支える MCP ツール群を提供します。計画の価値が低い明確な単発検索では、直接 `web_search` を呼ぶこともできます。より重い探索タスクについては、今後 `deep research` 方向へ拡張します。

公開されている package import contract には現在 2 つの境界があります。`grok_search.mcp` は access-time lazy export なので、その導出に実際にアクセスしたときだけ `fastmcp` が必要です。`grok_search.providers.GrokSearchProvider` も access-time lazy export なので、通常の非 provider import は Grok provider 関連依存が欠けているだけで早期に失敗すべきではありません。これは import-time の挙動を狭めるだけで、インストール時の依存宣言は変わりませんし、依存が optional extras になったことを意味しません。

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

`plan_search_term` は `search_strategy` の初回作成時に `approach` / `fallback_plan` を設定します。以後の非 `is_revision` 呼び出しは `search_terms` の追加だけを行い、既存の strategy metadata を暗黙に上書きしません。
planning `session_id` は現在のプロセス内だけで有効な transient handle であり、既定 TTL は約 1 時間、LRU 上限は 256 です。プロセス再起動、TTL 切れ、eviction 後は新しい `plan_intent` からやり直してください。
wrapper はあえて scalar shim 入力を保っており、`depends_on` は CSV、`parallel_groups` はセミコロン区切りの CSV、`params_json` は文字列化 JSON を受け取ります。最初の `plan_search_term` 呼び出しでは `approach` が必須です。

## インストール

### 前提条件

- Python `3.10+`
- `uv`
- stdio MCP をサポートするクライアント

### サポートレベル

- `Officially tested`: Claude Code（リポジトリ内で検証したローカル `stdio` 経路とプロジェクト設定経路を指し、完全なホスト E2E 行列を意味しません）
- `Community-tested`: Codex スタイルの MCP クライアント、Cherry Studio
- `Planned`: Dify、n8n、Coze

補足:

- 公開ドキュメントが保証するのは現在ローカル `stdio` 構成のみです。
- `toggle_builtin_tools` は Claude Code のプロジェクト設定専用です。
- `get_config_info` における `toggle_builtin_tools` の readiness は、ローカル Git プロジェクト文脈を検出したことだけを示し、Claude Code ホスト全体の検証ではありません。
- 以下のインストール例は、現在メンテナンスされている公開配布元 `Boulea7/GrokSearchTool` を使います。
- ローカル worktree、過去の remote 名、古い協業痕跡は、現在も `fork/upstream` PR フローを使っている証拠として読まないでください。

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
    "TAVILY_API_URL": "https://api.tavily.com",
    "FIRECRAWL_API_KEY": "fc-your-firecrawl-key"
  }
}'
```

システム証明書ストアが必要な環境では、`uvx` に `--native-tls` を追加してください。これは企業プロキシや自己署名証明書チェーン向けの起動/インストール層 TLS 回避策であり、一般的な実行時 `verify=false` の代替ではありません。

### 他の `stdio` ホスト向け最小設定

#### Codex CLI / Codex-style clients

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

プロジェクトレベルの `.codex/config.toml` を使う場合は、実際のキーをリポジトリにコミットしないでください。このリポジトリは `.codex/` を既定で無視します。ローカル開発では、機密値を無視対象の `.env.local` に置く運用を推奨します。

`grok-search` は `process env -> project .env.local -> project .env -> persisted config -> code defaults` の順で設定を自動解決するため、通常は `.env.local` を shell script として `source` する必要はありません。プロジェクト環境変数のフォールバックは `KEY=value` とオプションの `export KEY=value` の両方に対応します。現在の shell に変数を入れる必要がある場合は、blind に `source` せず shell-safe な明示的 export を使ってください。`toggle_builtin_tools` を使う場合も、プロジェクトレベルの `.claude/settings.json` はコミットしないでください。このリポジトリは `.claude/` も既定で無視します。

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

### 主要な環境変数

| 変数 | 必須 | 説明 |
| --- | --- | --- |
| `GROK_API_URL` | Yes | OpenAI 互換 Grok エンドポイント。明示的な `/v1` サフィックス付きのルートを推奨します。現在のコードパスは `/v1` 省略だけでは事前にブロックしませんが、多くの OpenAI 互換エンドポイントでは実行時に失敗し、通常は compatibility warning も伴います |
| `GROK_API_KEY` | Yes | Grok API Key |
| `GROK_MODEL` | No | デフォルトモデル。優先順位は process env > project `.env.local` > project `.env` > 永続 config > コード既定値 |
| `GROK_TIME_CONTEXT_MODE` | No | 時間コンテキスト注入モード：`always` / `auto` / `never` |
| `TAVILY_API_KEY` | No | `web_fetch` / `web_map` 用 Tavily Key。Tavily ベースの supplemental `web_search` にも使用 |
| `TAVILY_API_URL` | No | Tavily API エンドポイント |
| `TAVILY_ENABLED` | No | Tavily ルートを有効化するか |
| `FIRECRAWL_API_KEY` | No | Firecrawl fallback Key。supplemental `web_search` にも使用可能 |
| `FIRECRAWL_API_URL` | No | Firecrawl API エンドポイント |
| `GROK_DEBUG` | No | デバッグログと debug-only `ctx.info()` 進捗転送を有効化するか |
| `GROK_LOG_LEVEL` | No | ログレベル |
| `GROK_LOG_DIR` | No | ログディレクトリ。`get_config_info` は解決後の実行時パスを返す |
| `GROK_OUTPUT_CLEANUP` | No | `web_search` 出力クリーンアップを有効化するか |
| `GROK_FILTER_THINK_TAGS` | No | `GROK_OUTPUT_CLEANUP` の旧エイリアス |
| `GROK_RETRY_MAX_ATTEMPTS` | No | 最大リトライ回数 |
| `GROK_RETRY_MULTIPLIER` | No | リトライ時のバックオフ倍率 |
| `GROK_RETRY_MAX_WAIT` | No | 最大待機秒数 |

補足:

- モデル解決順はプロセス `GROK_MODEL` 環境変数 → プロジェクト `.env.local` → プロジェクト `.env` → `~/.config/grok-search/config.json` の永続値 → コード既定値 `grok-4.1-fast` です。OpenRouter 互換 URL を使う場合、必要に応じて `:online` が自動付与されます。
- 環境変数の優先は「キーが存在するか」で判定されます。プロセス環境に明示的にキーがある場合、値が空文字でもプロジェクト `.env.local` / `.env` にはフォールバックしません。
- `switch_model` は `~/.config/grok-search/config.json` の永続値のみを更新します。`GROK_MODEL` が設定されている場合は env が優先されます。
- `get_config_info` のベース設定スナップショットには `GROK_MODEL_SOURCE` も含まれ、現在のアクティブモデルをどの層が供給しているか（`process_env`、`project_env_local`、`project_env`、`persisted_config`、`default`）を確認できます。ここが `process_env`、`project_env_local`、`project_env` の場合、`switch_model` を単独で呼んでも現在のプロセスは切り替わりません。
- `GROK_TIME_CONTEXT_MODE` の既定値は `always` で、現在の「常にローカル時間を注入する」動作を維持します。
- `GROK_DEBUG=false` のとき、これらの helper progress log は logger にも `ctx.info()` にも流れません。`GROK_DEBUG=true` のときだけ debug-only progress/debug signal として転送されます。
- コンテキストを節約したい場合は、`GROK_TIME_CONTEXT_MODE` を `auto`（明確に時系列依存の問い合わせ時のみ注入）または `never` に変更できます。

補足:

- 推奨されるコア経路は `plan_* -> web_search` です。明確な単発検索では直接 `web_search` も利用できます。
- インタラクティブな `deep research` 体験は、MCP / skill ではなく CLI を優先して提供する予定です。
- `web_fetch` は Firecrawl のみでも動作します。
- `web_map` には Tavily と `TAVILY_ENABLED=true` が必要です。
- `web_search` は `GROK_TIME_CONTEXT_MODE` に従ってローカル時間コンテキストを注入します（既定値は `always`）。
- upstream endpoint が loopback を指す場合、そのリクエストでは `trust_env=False` が強制され、`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` と `SSL_CERT_FILE` / `SSL_CERT_DIR` も同時に無効になります。
- `get_config_info` はベース設定スナップショットと `connection_test` を維持しつつ、server 側で軽量な `doctor`、`feature_readiness`、最小の実検索/実取得プローブを追加します。ただし、完全なエンドツーエンド保証ではありません。
- `web_fetch`、`web_map`、および Tavily ベースの補助 `web_search` は、provider の全ネイティブ API ではなく、厳選した subset のみを公開します。
- `web_fetch` が返すのは抽出後の Markdown テキストであり、provider の完全な構造化 raw payload ではありません。
- Tavily `web_map` は外部ドメインの URL を含む場合があります。サイト内に近い map が必要な場合は、`instructions` と返却結果の後処理で絞り込んでください。
- `web_fetch` / `web_map` は、非 `http/https`、loopback、明らかな private network target、単一ラベル host、`.internal` / `.local` / `.lan` / `.home` / `.corp` のような代表的な private suffix host、`localtest.me` / `lvh.me` のような loopback helper domain、さらにローカル/私用 IP を公開 DNS 名に埋め込む alias（`nip.io` / `xip.io` / `sslip.io`）も既定で拒否します。
- 静的な URL 検査を通過した後も、`web_fetch` / `web_map` は provider 呼び出し前に可視な redirect 先を再検査します。
- 現在この可視 redirect 再検査は `HEAD` ではなく `GET` を使います。presigned URL、one-shot token、読み取り自体に副作用があるリンクでは、追加の事前取得が起き得る点を既知の境界として扱ってください。
- 現在の可視 redirect 再検査は最大 `5` 回までです。第 `5` 回の事前検査時点でも新しい可視 redirect が続く場合は、`目標 URL 重定向次数过多` として hard reject され、下流 provider 呼び出しへは進みません。
- redirect の事前検査で timeout または request-level error が起きた場合、現在の実装はその段階を `skipped_due_to_error` として扱います。`web_fetch` / `web_map` は現状では下流 provider 呼び出しを継続します。
- この境界は、ローカル DNS が公開ホスト風の名前を私用アドレスへ解決した場合まで強制的には拒否しないため、`best-effort safety boundary` として理解すべきであり、split-horizon やローカル DNS 汚染に対する hard-stop 保証ではありません。

### 最小 smoke check

任意のローカル `stdio` ホストでは、少なくとも次の順で確認してください。

1. `get_config_info` を呼び、ベース設定スナップショット、`connection_test`、`doctor`、`feature_readiness` が想定どおりか確認する。対応 provider 未設定時は追加の `search/fetch` probe が skip されても問題ない
2. `web_search` を 1 回実行する
3. ソース確認が必要なら `get_sources` を呼ぶ
4. Tavily / Firecrawl を設定している場合のみ `web_fetch` を検証し、`web_map` は Tavily を設定・有効化している場合のみ検証する

補足:

- `doctor.recommendations_detail` は `check_id` / feature に紐づく構造化された修復ヒントです。
- `get_config_info` は任意の `detail="full" | "summary"` を受け付けます。既定値は引き続き `full` で、`summary` はベース設定スナップショット、`connection_test`、`doctor.status/summary/recommendations`、`feature_readiness` のみを返します。
- `detail="summary"` は現時点では同じ診断実行結果のコンパクトな投影であり、別個の軽量実行パスではありません。
- `connection_test` は現時点では `/models` 到達性しか表しません。`web_search` が `degraded` の場合は、`doctor`、`feature_readiness`、`GROK_MODEL_SOURCE`、`grok_model_selection` / `grok_search_probe` を合わせて原因を判断してください。
- `feature_readiness.web_fetch.providers` には provider 単位の状態が含まれ、`verified_path` は実 fetch probe が通った backend を示します。skip された provider には `skipped_reason` が付く場合があります。
- `feature_readiness.get_sources` が `ready` になるのは、現在のプロセス内に少なくとも 1 つの非 error で読み出し可能な source session がある場合だけです。失敗検索だけが残っている場合は `partial_ready` のままです。
- API Key はマスクされますが、診断ペイロードにはローカル絶対パス、endpoint/hostname、短い upstream エラー要約が残る場合があります。明白な bearer/token/署名 query に加えて、`X-Amz-Credential`、`X-Goog-Credential`、`GoogleAccessId` のような高信頼 cloud-signed credential key もマスクされますが、外部共有前に確認してください。
- `get_sources` が成功したときは、常に `session_id`、`sources`、`sources_count`、`search_status`、`search_error`、`source_state` を返します。`session_id` が欠落または期限切れのときだけ `error=session_id_not_found_or_expired` が追加されます。
- `get_sources` は現在のサーバープロセス内にあるメモリ型 LRU キャッシュ（既定 TTL は約 1 時間、上限 256 session）を参照します。`session_id` は shared-daemon transient handle であり、durable でも caller-bound でも secret token でもありません。`session_id_not_found_or_expired` はプロセス再起動、TTL 切れ、eviction、読み出せない旧キャッシュ miss をまとめて表します。
- `sources_count` は現在、標準化・重複排除・フィルタ後に最終的にキャッシュへ書き込まれた source 数を表し、upstream の生 citation 件数そのものではありません。
- `get_sources` の `rank` は現在 `score`、source identity の明確さ、安定した dedupe 順に従い、Grok 由来の引用へ追加の優先度は与えません。`standardize_sources` は dedupe の際に scheme/host の大文字小文字差を正規化するため、同じページの mixed-case variant は 1 件に畳み込まれる場合があります。その一方で安全な fragment は保持し、URL の `userinfo`、代表的な署名パラメータ、そして `X-Amz-Credential`、`X-Goog-Credential`、`GoogleAccessId` のような高信頼 cloud-signed credential key は引き続き除去またはマスクします。明示的な既定ポート（`:443` / `:80`）は現時点では保持され、暗黙の既定ポート URL と自動では畳み込まれません。

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
