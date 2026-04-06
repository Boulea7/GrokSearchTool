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

- `Officially tested`: Claude Code
- `Community-tested`: MCP-клиенты в стиле Codex, Cherry Studio
- `Planned`: Dify, n8n, Coze

Примечания:

- Публичная документация пока обещает только локальный сценарий `stdio`.
- `toggle_builtin_tools` относится только к проектным настройкам Claude Code.
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
| `GROK_MODEL` | Нет | Модель по умолчанию; приоритет: env > persisted config > кодовый default |
| `GROK_TIME_CONTEXT_MODE` | Нет | Режим внедрения временного контекста: `always` / `auto` / `never` |
| `TAVILY_API_KEY` | Нет | Tavily key для `web_fetch` / `web_map` |
| `TAVILY_API_URL` | Нет | Tavily API endpoint |
| `TAVILY_ENABLED` | Нет | Включать ли Tavily-пути |
| `FIRECRAWL_API_KEY` | Нет | Firecrawl fallback key |
| `FIRECRAWL_API_URL` | Нет | Firecrawl API endpoint |
| `GROK_DEBUG` | Нет | Включить debug-логи |
| `GROK_LOG_LEVEL` | Нет | Уровень логирования |
| `GROK_LOG_DIR` | Нет | Каталог логов; `get_config_info` возвращает уже разрешённый runtime path |
| `GROK_OUTPUT_CLEANUP` | Нет | Включать ли очистку вывода `web_search` |
| `GROK_FILTER_THINK_TAGS` | Нет | Старый алиас для `GROK_OUTPUT_CLEANUP` |
| `GROK_RETRY_MAX_ATTEMPTS` | Нет | Максимальное число повторных попыток |
| `GROK_RETRY_MULTIPLIER` | Нет | Коэффициент backoff для retry |
| `GROK_RETRY_MAX_WAIT` | Нет | Максимальное время ожидания в секундах |

Примечания:

- Порядок разрешения модели: переменная окружения `GROK_MODEL` → сохранённое значение в `~/.config/grok-search/config.json` → кодовый default `grok-4.1-fast`. Для OpenRouter-совместимых URL при необходимости автоматически добавляется суффикс `:online`.
- `switch_model` обновляет только сохранённое значение в `~/.config/grok-search/config.json`; если задан `GROK_MODEL`, приоритет остаётся у env.
- `GROK_TIME_CONTEXT_MODE` по умолчанию равен `always`, то есть текущее поведение с постоянной инъекцией локального времени сохраняется.
- Если нужно экономить контекст, можно переключить `GROK_TIME_CONTEXT_MODE` в `auto` (инъекция только для явно временных запросов) или `never`.

Примечания:

- Рекомендуемый основной путь: `plan_* -> web_search`. Для ясных одношаговых запросов можно вызывать `web_search` напрямую.
- Интерактивный опыт `deep research` планируется прежде всего для CLI, а не для диалоговых MCP / skill-интеграций.
- `web_fetch` работает и только с Firecrawl.
- `web_map` требует Tavily и `TAVILY_ENABLED=true`.
- `web_search` добавляет локальный временной контекст в соответствии с `GROK_TIME_CONTEXT_MODE` (по умолчанию `always`).
- `get_config_info` сохраняет базовый снимок конфигурации и `connection_test`, а сервер дополнительно добавляет лёгкие представления `doctor`, `feature_readiness` и минимальные реальные `search/fetch` пробы; это всё ещё не полная end-to-end гарантия.

### Минимальный smoke check

Для любого локально настроенного `stdio`-хоста рекомендуется минимум такой порядок проверки:

1. вызвать `get_config_info` и убедиться, что базовый снимок конфигурации, `connection_test`, `doctor` и `feature_readiness` соответствуют целевой установке; дополнительные `search/fetch`-пробы могут быть пропущены, если provider не настроен
2. выполнить один `web_search`
3. вызвать `get_sources`, если важна проверка источников
4. проверять `web_fetch` только когда Tavily или Firecrawl уже настроены, а `web_map` — только когда Tavily настроен и включён

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
