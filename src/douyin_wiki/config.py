from __future__ import annotations

import os
import tomllib
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import AnalysisMode


def default_config_path() -> Path:
    override = os.environ.get("DOUYIN_WIKI_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "douyin-wiki" / "config.toml"


def normalize_llm_base_url(value: str) -> str:
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("模型接口必须是有效的 HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None or "#" in candidate:
        raise ValueError("模型接口不能包含凭据或 URL 片段")
    loopback = parsed.hostname.lower() == "localhost"
    if not loopback:
        try:
            loopback = ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
    if parsed.scheme == "http" and not loopback:
        raise ValueError("非本机模型接口必须使用 HTTPS")
    return candidate.rstrip("/")


class LLMSettings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    enabled: bool = True
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    api_key_env: str = "DOUYIN_WIKI_LLM_API_KEY"
    timeout_seconds: float = 120
    max_retries: int = 2

    @field_validator("base_url", mode="before")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        return normalize_llm_base_url(value)


class EmbeddingSettings(BaseModel):
    provider: str = "sentence-transformers"
    model: str = "BAAI/bge-small-zh-v1.5"
    fallback_dimensions: int = 384


class MediaSettings(BaseModel):
    browser: str = "chrome"
    browser_profile: str | None = None
    retention_days: int = 30
    cloud_confirmation_minutes: int = 30
    max_duration_minutes: int = 120
    frame_interval_seconds: int = 10
    scene_threshold: float = 0.35
    max_frames: int = 60
    whisper_provider: str = "auto"
    whisper_model: str = "mlx-community/whisper-large-v3-turbo"
    whisper_cli_model: str = "large-v3-turbo"


class WorkerSettings(BaseModel):
    poll_seconds: float = 2
    download_concurrency: int = 2
    media_concurrency: int = 1
    analysis_concurrency: int = 2
    lease_seconds: int = 180
    heartbeat_seconds: int = 30


class AuthGuidanceSettings(BaseModel):
    enabled: bool = True
    timeout_seconds: int = Field(default=600, ge=30, le=1800)
    poll_seconds: float = Field(default=5, ge=2, le=30)


class WebSettings(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)

    @model_validator(mode="after")
    def local_only(self) -> WebSettings:
        if self.host != "127.0.0.1":
            raise ValueError("Web 服务只允许绑定 127.0.0.1")
        return self


class AppConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    vault_path: Path = Path.home() / "Documents" / "Obsidian" / "抖库"
    timezone: str = "Asia/Shanghai"
    analysis_mode: AnalysisMode = AnalysisMode.GATEWAY
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embeddings: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    auth_guidance: AuthGuidanceSettings = Field(default_factory=AuthGuidanceSettings)
    web: WebSettings = Field(default_factory=WebSettings)

    @property
    def state_dir(self) -> Path:
        return self.vault_path / ".douyin-wiki"

    @property
    def database_path(self) -> Path:
        return self.state_dir / "state.sqlite3"

    @property
    def work_dir(self) -> Path:
        return self.state_dir / "work"

    @property
    def browser_profile_dir(self) -> Path:
        """Persistent profile used only by 抖库 browser automation."""
        override = os.environ.get("DOUYIN_WIKI_BROWSER_PROFILE")
        if override:
            return Path(override).expanduser()
        return Path.home() / "Library" / "Application Support" / "douyin-wiki" / "browser-profile"


def llm_api_key_required(base_url: str) -> bool:
    """Return whether an OpenAI-compatible endpoint should require an API key.

    Loopback endpoints are intentionally allowed without a key so local LM Studio and
    Ollama-compatible servers work without storing or transmitting a dummy credential.
    """
    hostname = (urlsplit(base_url).hostname or "").lower()
    if hostname == "localhost":
        return False
    try:
        return not ip_address(hostname).is_loopback
    except ValueError:
        return True


def llm_is_configured(settings: LLMSettings, api_key: str) -> bool:
    return bool(
        settings.enabled
        and settings.model
        and (api_key or not llm_api_key_required(settings.base_url))
    )


def _merge_dict(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or default_config_path()
    if not config_path.exists():
        return AppConfig()
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    return AppConfig.model_validate(data)


def _toml_string(value: object) -> str:
    """Encode a value as a TOML basic string using JSON-compatible escaping."""
    import json

    return json.dumps(str(value), ensure_ascii=False)


def render_default_config(config: AppConfig | None = None) -> str:
    cfg = config or AppConfig()
    profile = (
        f"\nbrowser_profile = {_toml_string(cfg.media.browser_profile)}"
        if cfg.media.browser_profile
        else ""
    )
    return f"""vault_path = {_toml_string(cfg.vault_path)}
timezone = {_toml_string(cfg.timezone)}
analysis_mode = {_toml_string(cfg.analysis_mode.value)}

[llm]
enabled = {str(cfg.llm.enabled).lower()}
base_url = {_toml_string(cfg.llm.base_url)}
model = {_toml_string(cfg.llm.model)}
api_key_env = {_toml_string(cfg.llm.api_key_env)}
timeout_seconds = {cfg.llm.timeout_seconds}
max_retries = {cfg.llm.max_retries}

[embeddings]
provider = {_toml_string(cfg.embeddings.provider)}
model = {_toml_string(cfg.embeddings.model)}
fallback_dimensions = {cfg.embeddings.fallback_dimensions}

[media]
browser = {_toml_string(cfg.media.browser)}{profile}
retention_days = {cfg.media.retention_days}
cloud_confirmation_minutes = {cfg.media.cloud_confirmation_minutes}
max_duration_minutes = {cfg.media.max_duration_minutes}
frame_interval_seconds = {cfg.media.frame_interval_seconds}
scene_threshold = {cfg.media.scene_threshold}
max_frames = {cfg.media.max_frames}
whisper_provider = {_toml_string(cfg.media.whisper_provider)}
whisper_model = {_toml_string(cfg.media.whisper_model)}
whisper_cli_model = {_toml_string(cfg.media.whisper_cli_model)}

[worker]
poll_seconds = {cfg.worker.poll_seconds}
download_concurrency = {cfg.worker.download_concurrency}
media_concurrency = {cfg.worker.media_concurrency}
analysis_concurrency = {cfg.worker.analysis_concurrency}
lease_seconds = {cfg.worker.lease_seconds}
heartbeat_seconds = {cfg.worker.heartbeat_seconds}

[auth_guidance]
enabled = {str(cfg.auth_guidance.enabled).lower()}
timeout_seconds = {cfg.auth_guidance.timeout_seconds}
poll_seconds = {cfg.auth_guidance.poll_seconds}

[web]
enabled = {str(cfg.web.enabled).lower()}
host = {_toml_string(cfg.web.host)}
port = {cfg.web.port}
"""
