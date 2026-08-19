from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .models import AnalysisMode


def default_config_path() -> Path:
    override = os.environ.get("DOUYIN_WIKI_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "douyin-wiki" / "config.toml"


class LLMSettings(BaseModel):
    enabled: bool = True
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    api_key_env: str = "DOUYIN_WIKI_LLM_API_KEY"
    timeout_seconds: float = 120
    max_retries: int = 2


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


class AppConfig(BaseModel):
    vault_path: Path = Path.home() / "Documents" / "Obsidian" / "Douyin-Wiki"
    timezone: str = "Asia/Shanghai"
    analysis_mode: AnalysisMode = AnalysisMode.GATEWAY
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embeddings: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)

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
        """Persistent profile used only by Douyin Wiki's browser automation."""
        return Path.home() / "Library" / "Application Support" / "douyin-wiki" / "browser-profile"


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


def render_default_config(config: AppConfig | None = None) -> str:
    cfg = config or AppConfig()
    profile = (
        f'\nbrowser_profile = "{cfg.media.browser_profile}"' if cfg.media.browser_profile else ""
    )
    return f'''vault_path = "{cfg.vault_path}"
timezone = "{cfg.timezone}"
analysis_mode = "{cfg.analysis_mode.value}"

[llm]
enabled = {str(cfg.llm.enabled).lower()}
base_url = "{cfg.llm.base_url}"
model = "{cfg.llm.model}"
api_key_env = "{cfg.llm.api_key_env}"
timeout_seconds = {cfg.llm.timeout_seconds}
max_retries = {cfg.llm.max_retries}

[embeddings]
provider = "{cfg.embeddings.provider}"
model = "{cfg.embeddings.model}"
fallback_dimensions = {cfg.embeddings.fallback_dimensions}

[media]
browser = "{cfg.media.browser}"{profile}
retention_days = {cfg.media.retention_days}
cloud_confirmation_minutes = {cfg.media.cloud_confirmation_minutes}
max_duration_minutes = {cfg.media.max_duration_minutes}
frame_interval_seconds = {cfg.media.frame_interval_seconds}
scene_threshold = {cfg.media.scene_threshold}
max_frames = {cfg.media.max_frames}
whisper_provider = "{cfg.media.whisper_provider}"
whisper_model = "{cfg.media.whisper_model}"
whisper_cli_model = "{cfg.media.whisper_cli_model}"

[worker]
poll_seconds = {cfg.worker.poll_seconds}
download_concurrency = {cfg.worker.download_concurrency}
media_concurrency = {cfg.worker.media_concurrency}
analysis_concurrency = {cfg.worker.analysis_concurrency}
lease_seconds = {cfg.worker.lease_seconds}
heartbeat_seconds = {cfg.worker.heartbeat_seconds}
'''
