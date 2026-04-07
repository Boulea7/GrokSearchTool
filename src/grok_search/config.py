import os
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_URL_PARAM_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "code",
    "token",
    "signature",
    "sig",
    "x-amz-credential",
    "x-amz-signature",
    "x-amz-security-token",
    "x-goog-credential",
    "x-goog-signature",
    "x-ms-signature",
    "googleaccessid",
}

class Config:
    _instance = None
    _SETUP_COMMAND = (
        'claude mcp add-json grok-search --scope user '
        '\'{"type":"stdio","command":"uvx","args":["--from",'
        '"git+https://github.com/Boulea7/GrokSearchTool@main","grok-search"],'
        '"env":{"GROK_API_URL":"https://your-api-endpoint.com/v1","GROK_API_KEY":"your-api-key"}}\''
    )
    _DEFAULT_MODEL = "grok-4.1-fast"

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._config_file = None
            cls._instance._cached_model = None
            cls._instance._project_env_cache = None
        return cls._instance

    def _project_root(self) -> Path:
        root = Path.cwd().resolve()
        while True:
            if (root / ".git").exists() or (root / "pyproject.toml").exists() or (root / "AGENTS.md").exists():
                return root
            if root == root.parent:
                return Path.cwd().resolve()
            root = root.parent

    def _parse_env_file(self, path: Path) -> dict[str, str]:
        parsed: dict[str, str] = {}
        if not path.exists():
            return parsed
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if not key:
                    continue
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
                parsed[key] = value
        except OSError:
            return {}
        return parsed

    def _load_project_env(self) -> dict[str, str]:
        if self._project_env_cache is not None:
            return self._project_env_cache

        project_root = self._project_root()
        merged: dict[str, str] = {}
        for name in (".env", ".env.local"):
            merged.update(self._parse_env_file(project_root / name))
        self._project_env_cache = merged
        return merged

    def _get_env_value(self, key: str, default: str | None = None) -> str | None:
        if key in os.environ:
            return os.environ[key]
        project_env = self._load_project_env()
        if key in project_env:
            return project_env[key]
        return default

    @property
    def config_file(self) -> Path:
        if self._config_file is None:
            config_dir = Path.home() / ".config" / "grok-search"
            try:
                config_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                config_dir = Path.cwd() / ".grok-search"
                config_dir.mkdir(parents=True, exist_ok=True)
            self._config_file = config_dir / "config.json"
        return self._config_file

    def _load_config_file(self) -> dict:
        if not self.config_file.exists():
            return {}
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save_config_file(self, config_data: dict) -> None:
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            raise ValueError(f"无法保存配置文件: {str(e)}")

    @property
    def debug_enabled(self) -> bool:
        return (self._get_env_value("GROK_DEBUG", "false") or "false").lower() in ("true", "1", "yes")

    @property
    def retry_max_attempts(self) -> int:
        return int(self._get_env_value("GROK_RETRY_MAX_ATTEMPTS", "3") or "3")

    @property
    def retry_multiplier(self) -> float:
        return float(self._get_env_value("GROK_RETRY_MULTIPLIER", "1") or "1")

    @property
    def retry_max_wait(self) -> int:
        return int(self._get_env_value("GROK_RETRY_MAX_WAIT", "10") or "10")

    @property
    def output_cleanup_enabled(self) -> bool:
        raw = self._get_env_value("GROK_OUTPUT_CLEANUP")
        if raw is None:
            raw = self._get_env_value("GROK_FILTER_THINK_TAGS", "true")
        return raw.lower() in ("true", "1", "yes")

    @property
    def time_context_mode(self) -> str:
        raw = (self._get_env_value("GROK_TIME_CONTEXT_MODE", "always") or "always").strip().lower()
        return raw if raw in {"always", "auto", "never"} else "always"

    @property
    def grok_api_url(self) -> str:
        url = self._get_env_value("GROK_API_URL")
        if not url:
            raise ValueError(
                f"Grok API URL 未配置！\n"
                f"请使用以下命令配置 MCP 服务器：\n{self._SETUP_COMMAND}"
            )
        return url

    @property
    def grok_api_key(self) -> str:
        key = self._get_env_value("GROK_API_KEY")
        if not key:
            raise ValueError(
                f"Grok API Key 未配置！\n"
                f"请使用以下命令配置 MCP 服务器：\n{self._SETUP_COMMAND}"
            )
        return key

    @property
    def tavily_enabled(self) -> bool:
        return (self._get_env_value("TAVILY_ENABLED", "true") or "true").lower() in ("true", "1", "yes")

    @property
    def tavily_api_url(self) -> str:
        return self._get_env_value("TAVILY_API_URL", "https://api.tavily.com") or "https://api.tavily.com"

    @property
    def tavily_api_key(self) -> str | None:
        return self._get_env_value("TAVILY_API_KEY")

    @property
    def firecrawl_api_url(self) -> str:
        return self._get_env_value("FIRECRAWL_API_URL", "https://api.firecrawl.dev/v2") or "https://api.firecrawl.dev/v2"

    @property
    def firecrawl_api_key(self) -> str | None:
        return self._get_env_value("FIRECRAWL_API_KEY")

    @property
    def log_level(self) -> str:
        return (self._get_env_value("GROK_LOG_LEVEL", "INFO") or "INFO").upper()

    @property
    def log_dir(self) -> Path:
        log_dir_str = self._get_env_value("GROK_LOG_DIR", "logs") or "logs"
        log_dir = Path(log_dir_str)
        if log_dir.is_absolute():
            return log_dir

        home_log_dir = Path.home() / ".config" / "grok-search" / log_dir_str
        try:
            home_log_dir.mkdir(parents=True, exist_ok=True)
            return home_log_dir
        except OSError:
            pass

        cwd_log_dir = Path.cwd() / log_dir_str
        try:
            cwd_log_dir.mkdir(parents=True, exist_ok=True)
            return cwd_log_dir
        except OSError:
            pass

        tmp_log_dir = Path("/tmp") / "grok-search" / log_dir_str
        tmp_log_dir.mkdir(parents=True, exist_ok=True)
        return tmp_log_dir

    def _apply_model_suffix(self, model: str) -> str:
        if not model:
            return model
        try:
            url = self.grok_api_url
        except ValueError:
            return model
        if "openrouter" in url and ":online" not in model:
            return f"{model}:online"
        return model

    @property
    def grok_model(self) -> str:
        if self._cached_model is not None:
            return self._cached_model

        env_model = self._get_env_value("GROK_MODEL")
        if env_model is not None:
            model = env_model
        else:
            model = self._load_config_file().get("model") or self._DEFAULT_MODEL
        self._cached_model = self._apply_model_suffix(model)
        return self._cached_model

    def set_model(self, model: str) -> None:
        config_data = self._load_config_file()
        config_data["model"] = model
        self._save_config_file(config_data)
        self._cached_model = None

    def reset_runtime_state(self) -> None:
        self._cached_model = None
        self._project_env_cache = None

    @staticmethod
    def _mask_api_key(key: str) -> str:
        """脱敏显示 API Key，只显示前后各 4 个字符"""
        if not key or len(key) <= 8:
            return "***"
        return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"

    @staticmethod
    def _mask_url(url: str) -> str:
        text = (url or "").strip()
        if not text:
            return text

        try:
            split = urlsplit(text)
        except ValueError:
            return text

        if split.scheme.lower() not in {"http", "https"} or not split.netloc:
            return text

        hostname = split.hostname or ""
        if not hostname:
            return text

        if ":" in hostname and not hostname.startswith("["):
            host = f"[{hostname}]"
        else:
            host = hostname
        netloc = f"{host}:{split.port}" if split.port is not None else host
        query = urlencode(
            [
                (key, "***" if key.lower() in _SENSITIVE_URL_PARAM_KEYS else value)
                for key, value in parse_qsl(split.query, keep_blank_values=True)
            ],
            doseq=True,
            safe="*",
        )
        fragment = split.fragment
        if fragment and any(token in fragment for token in ("=", "&")):
            fragment = urlencode(
                [
                    (key, "***" if key.lower() in _SENSITIVE_URL_PARAM_KEYS else value)
                    for key, value in parse_qsl(fragment, keep_blank_values=True)
                ],
                doseq=True,
                safe="*",
            )

        return urlunsplit((split.scheme, netloc, split.path, query, fragment))

    def get_config_info(self) -> dict:
        """Return the base config snapshot only; server-side doctor fields are added elsewhere."""
        try:
            api_url = self.grok_api_url
            api_key_raw = self.grok_api_key
            api_key_masked = self._mask_api_key(api_key_raw)
            config_status = "配置完整"
        except ValueError as e:
            api_url = "未配置"
            api_key_masked = "未配置"
            config_status = f"配置错误: {str(e)}"

        return {
            "GROK_API_URL": self._mask_url(api_url) if api_url != "未配置" else api_url,
            "GROK_API_KEY": api_key_masked,
            "GROK_MODEL": self.grok_model,
            "GROK_DEBUG": self.debug_enabled,
            "GROK_OUTPUT_CLEANUP": self.output_cleanup_enabled,
            "GROK_TIME_CONTEXT_MODE": self.time_context_mode,
            "GROK_LOG_LEVEL": self.log_level,
            "GROK_LOG_DIR": str(self.log_dir),
            "TAVILY_API_URL": self._mask_url(self.tavily_api_url),
            "TAVILY_ENABLED": self.tavily_enabled,
            "TAVILY_API_KEY": self._mask_api_key(self.tavily_api_key) if self.tavily_api_key else "未配置",
            "FIRECRAWL_API_URL": self._mask_url(self.firecrawl_api_url),
            "FIRECRAWL_API_KEY": self._mask_api_key(self.firecrawl_api_key) if self.firecrawl_api_key else "未配置",
            "config_status": config_status
        }

config = Config()
