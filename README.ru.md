[简体中文](README.md) | [繁體中文](README.zh-TW.md) | [English](README.en.md) | [日本語](README.ja.md) | Русский

# GrokSearch

GrokSearch — это независимо поддерживаемый MCP-сервер для ассистентов и клиентов, которым нужен быстрый, надёжный и подтверждаемый источниками веб-контекст.

Он объединяет поиск через `Grok` и извлечение контента через `Tavily` / `Firecrawl`, предоставляя лёгкий MCP-набор инструментов для поиска, проверки источников, выборочного извлечения страниц и рекомендуемого основного маршрута `plan_* -> web_search` для сложных запросов. Для ясных одношаговых запросов с низкой неоднозначностью также допустим прямой вызов `web_search`. Для более тяжёлых задач в будущем будет развиваться отдельное направление `deep research`.

Публичный package import contract сейчас имеет две границы: `grok_search.mcp` — это access-time lazy export, поэтому `fastmcp` требуется только при фактическом обращении к этому экспорту; `grok_search.providers.GrokSearchProvider` тоже является access-time lazy export, поэтому обычные non-provider импорты не должны падать заранее только из-за отсутствия зависимостей Grok provider. Это лишь сужает import-time поведение, не меняет декларацию зависимостей на этапе установки и не должно читаться как превращение package dependencies в optional extras.

## Обзор

- `web_search`: веб-поиск с кэшированием источников
- `get_sources`: получение кэшированных источников из `web_search`
- `web_fetch`: сначала Tavily, затем Firecrawl как fallback
- `web_map`: карта структуры сайта
- `plan_*`: поэтапное планирование для сложных или неоднозначных запросов
- `get_config_info`: проверка конфигурации, `/models` и лёгкий doctor
- `switch_model`: смена модели Grok по умолчанию
- `toggle_builtin_tools`: переключение встроенных WebSearch / WebFetch в Claude Code

Сейчас опубликовано `13` MCP-инструментов.

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

`plan_search_term` задаёт `approach` / `fallback_plan` при первом создании `search_strategy`; последующие вызовы без `is_revision` только добавляют `search_terms` и не переписывают существующие strategy metadata неявно.
planning `session_id` — это in-process transient handle с TTL около 1 часа и LRU-лимитом 256 сессий; после рестарта процесса, истечения TTL или eviction нужно начинать заново с нового `plan_intent`.
wrapper'ы намеренно сохраняют scalar shim-входы: `depends_on` передаётся как CSV, `parallel_groups` — как CSV с разделением групп через `;`, а `params_json` — как строковый JSON. Первый вызов `plan_search_term` обязан передавать `approach`.

## Установка

### Требования

- Python `3.10+`
- `uv`
- клиент с поддержкой stdio MCP

### Уровни поддержки

- `Officially tested`: Claude Code для проверенного в репозитории локального `stdio`-пути и проектного пути настроек, а не как полной host-level E2E матрицы
- `Community-tested`: MCP-клиенты в стиле Codex, Cherry Studio
- `Planned`: Dify, n8n, Coze

Примечания:

- Публичная документация пока обещает только локальный сценарий `stdio`.
- `toggle_builtin_tools` относится только к проектным настройкам Claude Code.
- readiness для `toggle_builtin_tools` в `get_config_info` означает только то, что обнаружен локальный Git-контекст проекта; это не полная проверка хоста Claude Code.
- Ниже используются актуальные публичные установочные ссылки из поддерживаемого репозитория `Boulea7/GrokSearchTool`.
- Локальные worktree, исторические имена remote или старые следы совместной работы не следует трактовать как признак того, что проект всё ещё ведётся через `fork/upstream` PR-процесс.

### Добавление как MCP

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

Если окружение требует системное хранилище сертификатов, добавьте `--native-tls` к аргументам `uvx`. Это TLS-обход на уровне запуска/установки для корпоративных прокси и self-signed цепочек, а не универсальная замена отключению проверки сертификатов во время выполнения.

### Минимальные `stdio`-примеры для других хостов

#### Codex CLI / клиенты в стиле Codex

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

Если вы используете проектный `.codex/config.toml`, не коммитьте в репозиторий реальные ключи. Этот репозиторий по умолчанию игнорирует `.codex/`. Для локальной разработки безопаснее держать секреты в игнорируемом `.env.local`.

`grok-search` автоматически разрешает конфигурацию в порядке `process env -> project .env.local -> project .env -> persisted config -> code defaults`, поэтому обычно не нужно `source`-ить `.env.local` как shell-скрипт. Откат к проектным env-файлам сейчас поддерживает и строки `KEY=value`, и опциональный префикс `export KEY=value`; если вам действительно нужно экспортировать переменные в текущий shell, используйте явный shell-safe workflow, а не слепой `source`. Если вы вызываете `toggle_builtin_tools`, также не коммитьте проектный `.claude/settings.json`; `.claude/` тоже игнорируется по умолчанию.

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

### Основные переменные окружения

| Переменная | Обязательна | Описание |
| --- | --- | --- |
| `GROK_API_URL` | Да | OpenAI-совместимый Grok endpoint; рекомендуется явно указывать суффикс `/v1`, текущая кодовая ветка не блокирует запрос заранее только из-за его отсутствия, но многие OpenAI-совместимые endpoint'ы без него всё равно могут завершиться ошибкой во время выполнения и обычно сопровождаются compatibility warning |
| `GROK_API_KEY` | Да | Grok API key |
| `GROK_MODEL` | Нет | Модель по умолчанию; приоритет: process env > project `.env.local` > project `.env` > persisted config > кодовый default |
| `GROK_TIME_CONTEXT_MODE` | Нет | Режим внедрения временного контекста: `always` / `auto` / `never` |
| `TAVILY_API_KEY` | Нет | Tavily key для `web_fetch` / `web_map`, а также для Tavily-backed supplemental `web_search` |
| `TAVILY_API_URL` | Нет | Tavily API endpoint |
| `TAVILY_ENABLED` | Нет | Включать ли Tavily-пути |
| `FIRECRAWL_API_KEY` | Нет | Firecrawl key для fallback fetch и optional supplemental `web_search` |
| `FIRECRAWL_API_URL` | Нет | Firecrawl API endpoint |
| `GROK_DEBUG` | Нет | Включить debug-логи и debug-only пересылку прогресса через `ctx.info()` |
| `GROK_LOG_LEVEL` | Нет | Уровень логирования |
| `GROK_LOG_DIR` | Нет | Каталог логов; `get_config_info` возвращает уже разрешённый runtime path |
| `GROK_OUTPUT_CLEANUP` | Нет | Включать ли очистку вывода `web_search` |
| `GROK_FILTER_THINK_TAGS` | Нет | Старый алиас для `GROK_OUTPUT_CLEANUP` |
| `GROK_RETRY_MAX_ATTEMPTS` | Нет | Максимальное число повторных попыток |
| `GROK_RETRY_MULTIPLIER` | Нет | Коэффициент backoff для retry |
| `GROK_RETRY_MAX_WAIT` | Нет | Максимальное время ожидания в секундах |

Примечания:

- Порядок разрешения модели: переменная окружения процесса `GROK_MODEL` → проектный `.env.local` → проектный `.env` → сохранённое значение в `~/.config/grok-search/config.json` → кодовый default `grok-4.20-0309`. Для OpenRouter-совместимых URL при необходимости автоматически добавляется суффикс `:online`.
- Приоритет env определяется по самому факту наличия ключа: если ключ явно присутствует в окружении процесса, даже пустое значение не даст откатиться к проектным `.env.local` / `.env`.
- Текущий встроенный предпочтительный default — `grok-4.20-0309`. Для семейства Grok 4.1+ runtime-выбор теперь сделан более гибким: если запрошенная модель отсутствует в `/models`, но доступна совместимая Grok 4.1+, система старается откатиться к ней, а не падать только из-за отличающегося суффикса.
- `switch_model` обновляет только сохранённое значение в `~/.config/grok-search/config.json`; если задан `GROK_MODEL`, приоритет остаётся у env.
- Базовый снимок `get_config_info` теперь также включает `GROK_MODEL_SOURCE`, чтобы было видно, какой слой сейчас задаёт активную модель (`process_env`, `project_env_local`, `project_env`, `persisted_config`, `default`). Если здесь стоит `process_env`, `project_env_local` или `project_env`, одного вызова `switch_model` недостаточно, чтобы изменить текущий процесс.
- В таком override-сценарии `switch_model` всё равно обновит сохранённую конфигурацию, но возвращаемый `current_model` останется текущей runtime-эффективной моделью. Поле `runtime_model_source` показывает, какой более приоритетный слой всё ещё активен.
- `GROK_TIME_CONTEXT_MODE` по умолчанию равен `always`, то есть текущее поведение с постоянной инъекцией локального времени сохраняется.
- При `GROK_DEBUG=false` эти helper progress logs не пишутся и не пересылаются через `ctx.info()`; они намеренно работают как debug-only progress/debug signal.
- Если нужно экономить контекст, можно переключить `GROK_TIME_CONTEXT_MODE` в `auto` (инъекция только для явно временных запросов) или `never`.

Примечания:

- Рекомендуемый основной путь: `plan_* -> web_search`. Для ясных одношаговых запросов можно вызывать `web_search` напрямую.
- Интерактивный опыт `deep research` планируется прежде всего для CLI, а не для диалоговых MCP / skill-интеграций.
- `web_fetch` работает и только с Firecrawl.
- `web_map` требует Tavily и `TAVILY_ENABLED=true`.
- `web_search` добавляет локальный временной контекст в соответствии с `GROK_TIME_CONTEXT_MODE` (по умолчанию `always`).
- Если upstream endpoint указывает на loopback, запрос принудительно выполняется с `trust_env=False`, а значит одновременно обходятся `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` и `SSL_CERT_FILE` / `SSL_CERT_DIR`.
- `get_config_info` сохраняет базовый снимок конфигурации и `connection_test`, а сервер дополнительно добавляет лёгкие представления `doctor`, `feature_readiness` и минимальные реальные `search/fetch` пробы; это всё ещё не полная end-to-end гарантия.
- `web_fetch`, `web_map` и Tavily-backed supplemental `web_search` публикуют только curated subset возможностей provider'ов, а не их полный нативный API surface.
- `web_fetch` возвращает извлечённый Markdown-текст, а не полный raw structured payload provider'а.
- Tavily `web_map` может включать URL внешних доменов; если нужен результат ближе к внутрисайтовой sitemap, сужайте обход через `instructions` и фильтруйте результат после получения.
- `web_fetch` / `web_map` по умолчанию отклоняют не-`http/https`, loopback, очевидные private-network targets, одноярлыковые host'ы, типичные private-suffix host'ы (`.internal` / `.local` / `.lan` / `.home` / `.corp`), loopback-helper домены вроде `localtest.me` / `lvh.me`, а также распространённые публичные DNS-alias'ы, в которые закодирован локальный/приватный IP (`nip.io` / `xip.io` / `sslip.io`).
- После статической проверки URL `web_fetch` / `web_map` также перепроверяют видимые redirect-цели до вызова provider.
- Сейчас эта видимая redirect-проверка использует `GET`, а не `HEAD`; для presigned URL, one-shot token или ссылок, где даже чтение может иметь побочный эффект, это означает возможный дополнительный preflight-read и должно рассматриваться как известная граница.
- Сейчас видимая redirect-проверка выполняется не более `5` раз; если на `5`-й проверке всё ещё появляется новый видимый redirect, запрос жёстко отклоняется с текущим контрактом `目标 URL 重定向次数过多`, и до downstream provider дело не доходит.
- Если redirect-preflight завершается timeout'ом или request-level ошибкой, текущая реализация помечает этот шаг как `skipped_due_to_error`; `web_fetch` / `web_map` сейчас всё ещё продолжают downstream-вызов provider.
- Эту границу сейчас следует понимать как `best-effort safety boundary`, а не как hard-stop гарантию против split-horizon или локально отравленного DNS, который резолвит публично выглядящий hostname в приватную цель.

### Минимальный smoke check

Для любого локально настроенного `stdio`-хоста рекомендуется минимум такой порядок проверки:

1. вызвать `get_config_info` и убедиться, что базовый снимок конфигурации, `connection_test`, `doctor` и `feature_readiness` соответствуют целевой установке; дополнительные `search/fetch`-пробы могут быть пропущены, если provider не настроен
2. выполнить один `web_search`
3. вызвать `get_sources`, если важна проверка источников
4. проверять `web_fetch` только когда Tavily или Firecrawl уже настроены, а `web_map` — только когда Tavily настроен и включён

Примечания:

- `doctor.recommendations_detail` даёт структурированные подсказки по исправлению, связанные с `check_id` и feature.
- `get_config_info` теперь принимает необязательный `detail="full" | "summary"`; по умолчанию остаётся `full`, а `summary` оставляет только базовый config snapshot, `connection_test`, `doctor.status/summary/recommendations` и `feature_readiness`.
- `detail="summary"` сейчас является компактной проекцией того же диагностического запуска, а не отдельным облегчённым execution path.
- `connection_test` сейчас отражает только достижимость `/models`; если `web_search` находится в состоянии `degraded`, нужно смотреть на `doctor`, `feature_readiness`, `GROK_MODEL_SOURCE` и проверки `grok_model_selection` / `grok_model_runtime_fallback` / `grok_search_probe` вместе.
- `grok_model_selection` означает, что модель оказалась неподходящей уже на стадии видимости `/models`, а `grok_model_runtime_fallback` означает, что реальный путь `/chat/completions` смог завершиться только после повторного runtime-fallback к другому кандидату Grok; оба check могут появиться одновременно.
- `feature_readiness.web_fetch.providers` содержит состояние по каждому provider; `verified_path` показывает backend, который прошёл реальный fetch-probe. Каждый provider item стабильно содержит `check_id`, при выводимом машинном диагнозе также включает `reason_code`, а для пропущенных provider может присутствовать `skipped_reason`.
- `feature_readiness.get_sources` показывает `ready` только тогда, когда в текущем процессе уже есть хотя бы один читаемый non-error source session; если в кэше остались только сессии от неуспешных поисков, статус остаётся `partial_ready`. Даже если `web_search` сейчас not ready, `get_sources` всё ещё может оставаться `ready`, когда читаемый cached session уже есть, а upstream-проблема будет отражена в `degraded_by`.
- Даже при маскировании API key диагностический payload всё ещё может содержать локальные абсолютные пути, endpoint/hostname и короткие сводки upstream-ошибок; при этом маскируются не только bearer/token/подписанные query, но и высокодостоверные cloud-signed credential key, такие как `X-Amz-Credential`, `X-Goog-Credential` и `GoogleAccessId`. Перед внешней публикацией payload всё равно стоит перепроверить.
- При успешном `get_sources` ответ всегда содержит `session_id`, `sources`, `sources_count`, `search_status`, `search_error` и `source_state`; только при отсутствии или истечении `session_id` дополнительно возвращается `error=session_id_not_found_or_expired`.
- `get_sources` сейчас читает из in-process memory-backed LRU cache на запущенном сервере (по умолчанию TTL около 1 часа, лимит 256 session). `session_id` здесь является shared-daemon transient handle, а не durable, caller-bound capability или secret token; `session_id_not_found_or_expired` покрывает рестарт процесса, истечение TTL, вытеснение и miss для нечитаемых legacy-cache записей.
- `sources_count` сейчас означает итоговое количество источников после стандартизации, дедупликации и фильтрации, записанное в кэш, а не сырое число upstream-citation'ов.
- `rank` в `get_sources` сейчас определяется по `score`, качеству идентичности источника и стабильному dedupe-порядку без дополнительного приоритета для цитат Grok. `standardize_sources` также canonicalize'ит регистр scheme/host при dedupe, поэтому mixed-case варианты одной и той же страницы могут схлопываться в один source; при этом безопасные URL fragment сохраняются, а `userinfo`, типичные подписи/токены и высокодостоверные cloud-signed credential key вроде `X-Amz-Credential`, `X-Goog-Credential` и `GoogleAccessId` по-прежнему удаляются или маскируются. Явные default-port значения вроде `:443` и `:80` сейчас сохраняются и не схлопываются автоматически с implicit-default URL.
- Каждая итоговая запись источника сейчас является lossy aggregate display row; если нужен contributor-level attribution, читайте additive `contributors`.
- `source` остаётся legacy-overloaded field: при отсутствии `origin_type` старые cache entries всё ещё могут использовать его как provider alias.

## Companion Skill

Репозиторий также включает companion skill: [`skills/research-with-grok-search`](skills/research-with-grok-search/SKILL.md)

```bash
mkdir -p ~/.codex/skills
ln -s /absolute/path/to/GrokSearch/skills/research-with-grok-search ~/.codex/skills/research-with-grok-search
```

## Разработка

```bash
PYTHONPATH=src uv run python -m grok_search.server
uv run --with pytest --with pytest-asyncio pytest -q
uv run --with ruff ruff check .
python3 -m py_compile src/grok_search/*.py src/grok_search/providers/*.py tests/*.py
```

## Документация

- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Compatibility](docs/COMPATIBILITY.md)
- [Roadmap](docs/ROADMAP.md)
- [Changelog](CHANGELOG.md)

## License

[MIT](LICENSE)
