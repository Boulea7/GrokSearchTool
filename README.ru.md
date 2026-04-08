[简体中文](README.md) | [繁體中文](README.zh-TW.md) | [English](README.en.md) | [日本語](README.ja.md) | Русский

# GrokSearch

GrokSearch — это независимо поддерживаемый MCP-сервер для ассистентов и клиентов, которым нужен быстрый, надёжный и подтверждаемый источниками веб-контекст.

Он объединяет поиск через `Grok` и извлечение контента через `Tavily` / `Firecrawl`, предоставляя лёгкий MCP-набор инструментов для поиска, проверки источников, выборочного извлечения страниц и рекомендуемого основного маршрута `plan_* -> web_search` для сложных запросов. Для ясных одношаговых запросов с низкой неоднозначностью также допустим прямой вызов `web_search`. Для более тяжёлых задач в будущем будет развиваться отдельное направление `deep research`.

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

Если вы используете проектный `.codex/config.toml`, не коммитьте в репозиторий реальные ключи. Этот репозиторий по умолчанию игнорирует `.codex/`. Для локальной разработки безопаснее держать секреты в игнорируемом `.env.local` и загружать их перед запуском через `source ./.env.local`. Откат к проектным env-файлам сейчас поддерживает и строки `KEY=value`, и опциональный префикс `export KEY=value`. Если вы вызываете `toggle_builtin_tools`, также не коммитьте проектный `.claude/settings.json`; `.claude/` тоже игнорируется по умолчанию.

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
| `GROK_API_URL` | Да | OpenAI-совместимый Grok endpoint, желательно с явным `/v1` |
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

- Порядок разрешения модели: переменная окружения процесса `GROK_MODEL` → проектный `.env.local` → проектный `.env` → сохранённое значение в `~/.config/grok-search/config.json` → кодовый default `grok-4.1-fast`. Для OpenRouter-совместимых URL при необходимости автоматически добавляется суффикс `:online`.
- Приоритет env определяется по самому факту наличия ключа: если ключ явно присутствует в окружении процесса, даже пустое значение не даст откатиться к проектным `.env.local` / `.env`.
- `switch_model` обновляет только сохранённое значение в `~/.config/grok-search/config.json`; если задан `GROK_MODEL`, приоритет остаётся у env.
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
- `web_fetch` / `web_map` по умолчанию отклоняют не-`http/https`, loopback, очевидные private-network targets, одноярлыковые host'ы, типичные private-suffix host'ы (`.internal` / `.local` / `.lan` / `.home` / `.corp`) и распространённые публичные DNS-alias'ы, в которые закодирован локальный/приватный IP (`nip.io` / `xip.io` / `sslip.io`).
- После статической проверки URL `web_fetch` / `web_map` также перепроверяют видимые redirect-цели до вызова provider.
- Сейчас эта видимая redirect-проверка использует `GET`, а не `HEAD`; для presigned URL, one-shot token или ссылок, где даже чтение может иметь побочный эффект, это означает возможный дополнительный preflight-read и должно рассматриваться как известная граница.
- Эта граница сейчас не даёт жёсткой гарантии против split-horizon или локально отравленного DNS, который резолвит публично выглядящий hostname в приватную цель.

### Минимальный smoke check

Для любого локально настроенного `stdio`-хоста рекомендуется минимум такой порядок проверки:

1. вызвать `get_config_info` и убедиться, что базовый снимок конфигурации, `connection_test`, `doctor` и `feature_readiness` соответствуют целевой установке; дополнительные `search/fetch`-пробы могут быть пропущены, если provider не настроен
2. выполнить один `web_search`
3. вызвать `get_sources`, если важна проверка источников
4. проверять `web_fetch` только когда Tavily или Firecrawl уже настроены, а `web_map` — только когда Tavily настроен и включён

Примечания:

- `doctor.recommendations_detail` даёт структурированные подсказки по исправлению, связанные с `check_id` и feature.
- `get_config_info` теперь принимает необязательный `detail="full" | "summary"`; по умолчанию остаётся `full`, а `summary` оставляет только базовый config snapshot, `connection_test`, `doctor.status/summary/recommendations` и `feature_readiness`.
- `feature_readiness.web_fetch.providers` содержит состояние по каждому provider; `verified_path` показывает backend, который прошёл реальный fetch-probe, а для пропущенных provider может присутствовать `skipped_reason`.
- Даже при маскировании API key диагностический payload всё ещё может содержать локальные абсолютные пути, endpoint/hostname и короткие сводки upstream-ошибок; перед внешней публикацией его стоит перепроверить.
- `get_sources` сейчас читает из in-process memory-backed LRU cache на запущенном сервере (по умолчанию TTL около 1 часа, лимит 256 session). `session_id` здесь является transient handle, а не durable или caller-bound capability; после перезапуска процесса, истечения TTL или вытеснения старых записей он перестанет работать.
- `rank` в `get_sources` сейчас сохраняет приоритет за цитатами Grok, оставляет безопасные URL fragment и одновременно удаляет `userinfo` и маскирует типичные подписи/токены в URL.

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
