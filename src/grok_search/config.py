import os
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any

_SENSITIVE_URL_PARAM_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "client_secret",
    "code",
    "id_token",
    "password",
    "refresh_token",
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
        '"env":{"GROK_API_URL":"https://api.example.com/v1","GROK_API_KEY":"your-api-key"}}\''
    )
    _DEFAULT_MODEL = "grok-4.20-0309"
    _DEFAULT_MODEL_PROFILE = "balanced_auto"
    _DEFAULT_DEEP_RESEARCH_STANDARD_PROFILE = "reasoning"
    _DEFAULT_DEEP_RESEARCH_DEEP_PROFILE = "multi_agent"
    _KNOWN_PROVIDER_FAMILIES = {
        "official_xai",
        "openrouter",
        "openai_compatible_relay",
        "grok2api_like",
    }

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._config_file = None
            cls._instance._cached_model = None
            cls._instance._project_env_cache = None
            cls._instance._project_env_source_cache = None
            cls._instance._project_env_layers_cache = None
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
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                    if "=" not in line:
                        continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if not key:
                    continue
                value = self._normalize_env_value(value)
                parsed[key] = value
        except OSError:
            return {}
        return parsed

    @staticmethod
    def _normalize_env_value(value: str) -> str:
        text = (value or "").strip()
        if not text:
            return ""

        if text[0] in {'"', "'"}:
            quote = text[0]
            closing_index = 1
            while closing_index < len(text):
                if text[closing_index] == quote and text[closing_index - 1] != "\\":
                    remainder = text[closing_index + 1 :].strip()
                    if not remainder or remainder.startswith("#"):
                        return text[1:closing_index]
                    return text
                closing_index += 1
            return text

        return re.sub(r"\s+#.*$", "", text).strip()

    def _load_project_env_with_sources(self) -> tuple[dict[str, str], dict[str, str]]:
        if self._project_env_cache is not None:
            return self._project_env_cache, self._project_env_source_cache or {}

        project_root = self._project_root()
        merged: dict[str, str] = {}
        sources: dict[str, str] = {}
        for name in (".env", ".env.local"):
            current = self._parse_env_file(project_root / name)
            merged.update(current)
            source_name = "project_env_local" if name == ".env.local" else "project_env"
            for key in current:
                sources[key] = source_name
        self._project_env_cache = merged
        self._project_env_source_cache = sources
        return merged, sources

    def _load_project_env_layers(self) -> list[tuple[str, dict[str, str]]]:
        if self._project_env_layers_cache is not None:
            return self._project_env_layers_cache

        project_root = self._project_root()
        layers = [
            ("project_env_local", self._parse_env_file(project_root / ".env.local")),
            ("project_env", self._parse_env_file(project_root / ".env")),
        ]
        self._project_env_layers_cache = layers
        return layers

    def _load_project_env(self) -> dict[str, str]:
        merged, _ = self._load_project_env_with_sources()
        return merged

    def _get_env_value(self, key: str, default: str | None = None) -> str | None:
        if key in os.environ:
            return os.environ[key]
        project_env = self._load_project_env()
        if key in project_env:
            return project_env[key]
        return default

    def _get_env_value_source(self, key: str) -> str | None:
        if key in os.environ:
            return "process_env"
        _, project_sources = self._load_project_env_with_sources()
        return project_sources.get(key)

    @staticmethod
    def _provider_env_keys(suffix: int | None = None) -> tuple[str, str, str]:
        suffix_text = f"_{suffix}" if suffix is not None else ""
        return (
            f"GROK_API_URL{suffix_text}",
            f"GROK_API_KEY{suffix_text}",
            f"GROK_MODEL{suffix_text}",
        )

    @staticmethod
    def _provider_family_env_key(suffix: int | None = None) -> str:
        suffix_text = f"_{suffix}" if suffix is not None else ""
        return f"GROK_PROVIDER_FAMILY{suffix_text}"

    def _resolve_provider_credentials(self, suffix: int | None = None) -> dict[str, str] | None:
        url_key, key_key, _ = self._provider_env_keys(suffix)
        if url_key in os.environ or key_key in os.environ:
            return {
                "source": "process_env",
                "api_url": os.environ.get(url_key, ""),
                "api_key": os.environ.get(key_key, ""),
            }

        for source, values in self._load_project_env_layers():
            if url_key in values or key_key in values:
                return {
                    "source": source,
                    "api_url": values.get(url_key, ""),
                    "api_key": values.get(key_key, ""),
                }
        return None

    def _provider_family_override(self, suffix: int | None = None) -> str:
        raw = self._get_env_value(self._provider_family_env_key(suffix), "") or ""
        family = raw.strip().lower()
        return family if family in self._KNOWN_PROVIDER_FAMILIES else ""

    @staticmethod
    def _normalize_host(host: str) -> str:
        return (host or "").strip().lower().rstrip(".")

    def provider_family_for_url(self, api_url: str, *, suffix: int | None = None) -> str:
        override = self._provider_family_override(suffix)
        if override:
            return override
        try:
            host = self._normalize_host(urlsplit((api_url or "").strip()).hostname or "")
        except ValueError:
            host = ""
        if not host:
            return "openai_compatible_relay"
        if "openrouter.ai" in host:
            return "openrouter"
        if host == "api.x.ai" or host.endswith(".x.ai"):
            return "official_xai"
        if any(marker in host for marker in ("grok2api", "oneapi", "newapi", "example-provider")):
            return "grok2api_like"
        return "openai_compatible_relay"

    def grok_model_profile(self) -> str:
        raw = (self._get_env_value("GROK_MODEL_PROFILE", self._DEFAULT_MODEL_PROFILE) or "").strip().lower()
        allowed = {"balanced_auto", "reasoning", "multi_agent", "fast", "exact"}
        return raw if raw in allowed else self._DEFAULT_MODEL_PROFILE

    def grok_deep_research_standard_profile(self) -> str:
        raw = (
            self._get_env_value(
                "GROK_DEEP_RESEARCH_STANDARD_PROFILE",
                self._DEFAULT_DEEP_RESEARCH_STANDARD_PROFILE,
            )
            or ""
        ).strip().lower()
        allowed = {"balanced_auto", "reasoning", "multi_agent", "fast", "exact"}
        return raw if raw in allowed else self._DEFAULT_DEEP_RESEARCH_STANDARD_PROFILE

    def grok_deep_research_deep_profile(self) -> str:
        raw = (
            self._get_env_value(
                "GROK_DEEP_RESEARCH_DEEP_PROFILE",
                self._DEFAULT_DEEP_RESEARCH_DEEP_PROFILE,
            )
            or ""
        ).strip().lower()
        allowed = {"balanced_auto", "reasoning", "multi_agent", "fast", "exact"}
        return raw if raw in allowed else self._DEFAULT_DEEP_RESEARCH_DEEP_PROFILE

    def _resolved_default_model_for_family(self, provider_family: str, *, profile: str) -> str:
        family_defaults = {
            "official_xai": {
                "balanced_auto": "grok-4.20-0309-non-reasoning",
                "reasoning": "grok-4.20-0309-reasoning",
                "multi_agent": "grok-4.20-multi-agent-0309",
                "fast": "grok-4-1-fast-non-reasoning",
                "exact": self._DEFAULT_MODEL,
            },
            "openrouter": {
                "balanced_auto": "x-ai/grok-4.1-fast",
                "reasoning": "x-ai/grok-4.20",
                "multi_agent": "x-ai/grok-4.20-multi-agent",
                "fast": "x-ai/grok-4.1-fast",
                "exact": "x-ai/grok-4.20",
            },
            "openai_compatible_relay": {
                "balanced_auto": "grok-4.20-auto",
                "reasoning": "grok-4.20-reasoning",
                "multi_agent": "grok-4.20-multi-agent",
                "fast": "grok-4.20-fast",
                "exact": self._DEFAULT_MODEL,
            },
            "grok2api_like": {
                "balanced_auto": "grok-4.20-auto",
                "reasoning": "grok-4.20-reasoning",
                "multi_agent": "grok-4.20-multi-agent",
                "fast": "grok-4.20-fast",
                "exact": self._DEFAULT_MODEL,
            },
        }
        selected_family = family_defaults.get(provider_family, family_defaults["openai_compatible_relay"])
        return selected_family.get(profile, selected_family["balanced_auto"])

    def resolve_default_grok_model_for_url(
        self,
        api_url: str,
        *,
        profile: str | None = None,
        suffix: int | None = None,
    ) -> str:
        selected_profile = (profile or self.grok_model_profile()).strip().lower()
        if selected_profile == "exact":
            model = self._DEFAULT_MODEL
        else:
            provider_family = self.provider_family_for_url(api_url, suffix=suffix)
            model = self._resolved_default_model_for_family(provider_family, profile=selected_profile)
        return self._apply_model_suffix_for_url(model, api_url)

    def _has_explicit_runtime_model(self) -> bool:
        if self._get_env_value("GROK_MODEL") is not None:
            return True
        return bool(self._load_config_file().get("model"))

    def resolve_deep_research_model_for_url(self, api_url: str, *, effort: str) -> str:
        selected_profile = (
            self.grok_deep_research_deep_profile()
            if (effort or "").strip().lower() == "deep"
            else self.grok_deep_research_standard_profile()
        )
        return self.resolve_default_grok_model_for_url(api_url, profile=selected_profile)

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
    def deep_research_dir(self) -> Path:
        raw = self._get_env_value("GROK_DEEP_RESEARCH_DIR")
        if raw:
            return Path(raw).expanduser()
        return Path.home() / ".config" / "grok-search" / "deep-research"

    @property
    def deep_research_default_budget_seconds(self) -> int:
        raw = self._get_env_value("GROK_DEEP_RESEARCH_DEFAULT_BUDGET_SECONDS", "240") or "240"
        try:
            return max(60, int(raw))
        except ValueError:
            return 240

    @property
    def deep_research_hard_timeout_seconds(self) -> int:
        raw = self._get_env_value("GROK_DEEP_RESEARCH_HARD_TIMEOUT_SECONDS", "600") or "600"
        try:
            return max(120, int(raw))
        except ValueError:
            return 600

    @property
    def deep_research_max_concurrency(self) -> int:
        raw = self._get_env_value("GROK_DEEP_RESEARCH_MAX_CONCURRENCY", "3") or "3"
        try:
            return max(1, int(raw))
        except ValueError:
            return 3

    @property
    def deep_research_recent_reuse_seconds(self) -> int:
        raw = self._get_env_value("GROK_DEEP_RESEARCH_RECENT_REUSE_SECONDS", "1800") or "1800"
        try:
            return max(0, int(raw))
        except ValueError:
            return 1800

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
        provider = self._resolve_provider_credentials()
        url = provider["api_url"] if provider is not None else None
        if not url:
            raise ValueError(
                f"Grok API URL 未配置！\n"
                f"请使用以下命令配置 MCP 服务器：\n{self._SETUP_COMMAND}"
            )
        return url

    @property
    def grok_api_key(self) -> str:
        provider = self._resolve_provider_credentials()
        key = provider["api_key"] if provider is not None else None
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
        log_dir = self._resolved_log_dir_path()
        if Path(self._log_dir_setting()).is_absolute():
            return log_dir

        for candidate in self._log_dir_candidates():
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                return candidate
            except OSError:
                pass

        fallback = Path("/tmp") / "grok-search" / ((self._get_env_value("GROK_LOG_DIR", "logs") or "logs"))
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

    def _log_dir_setting(self) -> str:
        log_dir_str = self._get_env_value("GROK_LOG_DIR", "logs") or "logs"
        return log_dir_str

    def _resolved_log_dir_path(self) -> Path:
        log_dir_str = self._log_dir_setting()
        log_dir = Path(log_dir_str)
        if log_dir.is_absolute():
            return log_dir

        return Path.home() / ".config" / "grok-search" / log_dir_str

    def _log_dir_candidates(self) -> tuple[Path, ...]:
        log_dir_str = self._log_dir_setting()
        preferred = self._resolved_log_dir_path()
        if Path(log_dir_str).is_absolute():
            return (preferred,)

        return (
            preferred,
            Path.cwd() / log_dir_str,
            Path("/tmp") / "grok-search" / log_dir_str,
        )

    def _apply_model_suffix(self, model: str) -> str:
        if not model:
            return model
        try:
            url = self.grok_api_url
        except ValueError:
            return model
        return self._apply_model_suffix_for_url(model, url)

    @staticmethod
    def _apply_model_suffix_for_url(model: str, api_url: str) -> str:
        if not model:
            return model
        if "openrouter" in api_url.lower() and ":online" not in model:
            return f"{model}:online"
        return model

    def _raw_env_keys(self) -> set[str]:
        project_env = self._load_project_env()
        return set(os.environ) | set(project_env)

    def grok_provider_chain(self, model_override: str | None = None) -> list[dict[str, Any]]:
        primary_url = self.grok_api_url
        primary_key = self.grok_api_key
        explicit_runtime_model = self._has_explicit_runtime_model()
        if model_override is None:
            base_model = self.grok_model
        else:
            base_model = model_override
        chain: list[dict[str, Any]] = [
            {
                "name": "primary",
                "api_url": primary_url,
                "api_key": primary_key,
                "model": self._apply_model_suffix_for_url(base_model, primary_url),
                "provider_family": self.provider_family_for_url(primary_url),
                "source": "primary",
            }
        ]
        seen: set[tuple[str, str, str]] = {
            (chain[0]["api_url"], chain[0]["api_key"], chain[0]["model"])
        }
        suffixes = sorted(
            {
                int(match.group(1))
                for key in self._raw_env_keys()
                if (match := re.fullmatch(r"GROK_API_URL_(\d+)", key))
            }
        )
        for suffix in suffixes:
            provider = self._resolve_provider_credentials(suffix)
            provider_url = provider["api_url"] if provider is not None else None
            provider_key = provider["api_key"] if provider is not None else None
            if not provider_url or not provider_key:
                continue
            provider_model = self._get_env_value(f"GROK_MODEL_{suffix}")
            if provider_model is not None:
                resolved_model = self._apply_model_suffix_for_url(provider_model, provider_url)
            elif model_override is not None or explicit_runtime_model:
                resolved_model = self._apply_model_suffix_for_url(base_model, provider_url)
            else:
                resolved_model = self.resolve_default_grok_model_for_url(provider_url, suffix=suffix)
            identity = (provider_url, provider_key, resolved_model)
            if identity in seen:
                continue
            seen.add(identity)
            chain.append(
                {
                    "name": f"provider_{suffix}",
                    "api_url": provider_url,
                    "api_key": provider_key,
                    "model": resolved_model,
                    "provider_family": self.provider_family_for_url(provider_url, suffix=suffix),
                    "source": provider["source"],
                }
            )
        return chain

    @property
    def grok_model(self) -> str:
        if self._cached_model is not None:
            return self._cached_model

        env_model = self._get_env_value("GROK_MODEL")
        if env_model is not None:
            model = env_model
        else:
            persisted_model = self._load_config_file().get("model")
            if persisted_model:
                model = persisted_model
            else:
                try:
                    model = self.resolve_default_grok_model_for_url(self.grok_api_url)
                except ValueError:
                    model = self._DEFAULT_MODEL
        self._cached_model = self._apply_model_suffix(model)
        return self._cached_model

    @property
    def grok_model_source(self) -> str:
        env_source = self._get_env_value_source("GROK_MODEL")
        if env_source:
            return env_source
        if self._load_config_file().get("model"):
            return "persisted_config"
        return "default"

    def set_model(self, model: str) -> None:
        config_data = self._load_config_file()
        config_data["model"] = model
        self._save_config_file(config_data)
        self._cached_model = None

    def reset_runtime_state(self) -> None:
        self._cached_model = None
        self._project_env_cache = None
        self._project_env_source_cache = None
        self._project_env_layers_cache = None

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
        raw_port = ""
        hostinfo = split.netloc.rsplit("@", 1)[-1]
        if hostinfo.startswith("["):
            closing_idx = hostinfo.find("]")
            if closing_idx != -1 and closing_idx + 1 < len(hostinfo) and hostinfo[closing_idx + 1] == ":":
                raw_port = hostinfo[closing_idx + 2 :]
        else:
            if ":" in hostinfo:
                raw_port = hostinfo.rsplit(":", 1)[-1]
        if raw_port:
            netloc = f"{host}:{raw_port}"
        else:
            netloc = host
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
            "GROK_MODEL_SOURCE": self.grok_model_source,
            "GROK_MODEL_PROFILE": self.grok_model_profile(),
            "GROK_DEEP_RESEARCH_STANDARD_PROFILE": self.grok_deep_research_standard_profile(),
            "GROK_DEEP_RESEARCH_DEEP_PROFILE": self.grok_deep_research_deep_profile(),
            "GROK_PROVIDER_FAMILY": (
                self.provider_family_for_url(api_url) if api_url != "未配置" else "未配置"
            ),
            "GROK_DEBUG": self.debug_enabled,
            "GROK_OUTPUT_CLEANUP": self.output_cleanup_enabled,
            "GROK_TIME_CONTEXT_MODE": self.time_context_mode,
            "GROK_LOG_LEVEL": self.log_level,
            "GROK_LOG_DIR": str(self._resolved_log_dir_path()),
            "TAVILY_API_URL": self._mask_url(self.tavily_api_url),
            "TAVILY_ENABLED": self.tavily_enabled,
            "TAVILY_API_KEY": self._mask_api_key(self.tavily_api_key) if self.tavily_api_key else "未配置",
            "FIRECRAWL_API_URL": self._mask_url(self.firecrawl_api_url),
            "FIRECRAWL_API_KEY": self._mask_api_key(self.firecrawl_api_key) if self.firecrawl_api_key else "未配置",
            "config_status": config_status
        }

config = Config()
