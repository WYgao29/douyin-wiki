from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import (
    AppConfig,
    default_config_path,
    llm_api_key_required,
    llm_is_configured,
    render_default_config,
)
from .models import AnalysisMode
from .secrets import get_secret


class VaultSetupMode(StrEnum):
    NEW = "new"
    EXISTING = "existing"


def validate_vault_target(path: Path, mode: VaultSetupMode) -> Path:
    """Resolve and validate a user-selected Vault without modifying it."""
    target = path.expanduser().resolve()
    if target in {Path("/"), Path.home().resolve()}:
        raise ValueError("Vault 不能使用磁盘根目录或用户主目录")
    if target.exists() and not target.is_dir():
        raise ValueError(f"Vault 路径不是目录：{target}")
    if mode == VaultSetupMode.EXISTING and not target.is_dir():
        raise ValueError(f"已有 Vault 不存在：{target}")
    if mode == VaultSetupMode.NEW and target.is_dir():
        initialized = (target / ".douyin-wiki").is_dir()
        allowed = {".DS_Store", ".git", ".gitignore", ".obsidian"}
        unexpected = {item.name for item in target.iterdir()} - allowed
        if unexpected and not initialized:
            raise ValueError(
                f"新 Vault 目录不是空目录；请选择“使用已有 Vault”，或换一个新目录：{target}"
            )
    return target


def default_obsidian_registry_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"


def obsidian_vault_status(vault_path: Path, *, registry_path: Path | None = None) -> dict[str, Any]:
    """Report whether Obsidian has registered the selected directory as a Vault."""
    target = vault_path.expanduser().resolve()
    registry = registry_path or default_obsidian_registry_path()
    registered = False
    registry_error: str | None = None
    try:
        payload = json.loads(registry.read_text(encoding="utf-8"))
        vaults = payload.get("vaults", {})
        records = vaults.values() if isinstance(vaults, dict) else []
        registered = any(
            Path(record.get("path", "")).expanduser().resolve() == target
            for record in records
            if isinstance(record, dict) and record.get("path")
        )
    except FileNotFoundError:
        registry_error = "未找到 Obsidian Vault 注册表"
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        registry_error = f"无法读取 Obsidian Vault 注册表：{exc}"

    action = None
    if not registered:
        action = (
            f"打开 Obsidian → 管理仓库（Manage vaults）→ 打开本地文件夹作为仓库，选择：{target}"
        )
    return {
        "ok": registered,
        "registered": registered,
        "path": str(target),
        "registry_path": str(registry),
        "message": (
            "已在 Obsidian 中登记" if registered else registry_error or "尚未在 Obsidian 中登记"
        ),
        "action": action,
    }


def write_config(config: AppConfig, path: Path | None = None, *, overwrite: bool = False) -> Path:
    target = path or default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        return target
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(render_default_config(config))
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        temporary_path.replace(target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return target


def doctor(config: AppConfig) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    checks["analysis_mode"] = {
        "ok": True,
        "mode": config.analysis_mode.value,
        "message": {
            AnalysisMode.GATEWAY: "校正与分析由 OpenClaw/Hermes 等 Gateway Agent 完成",
            AnalysisMode.PROVIDER: "校正与分析由后台 OpenAI-compatible provider 完成",
            AnalysisMode.LOCAL: "校正与分析使用无 token 的本地降级逻辑",
        }[config.analysis_mode],
    }
    for command in ("yt-dlp", "ffmpeg", "ffprobe", "whisper", "swift", "osascript", "git"):
        checks[command] = {"ok": bool(shutil.which(command)), "path": shutil.which(command)}
    playwright_available = importlib.util.find_spec("playwright") is not None
    checks["playwright"] = {
        "ok": playwright_available,
        "message": None if playwright_available else "未安装 Playwright；请运行 uv sync",
    }
    web_dependencies = ("fastapi", "uvicorn", "jinja2", "markdown_it", "nh3", "watchfiles")
    missing_web = [name for name in web_dependencies if importlib.util.find_spec(name) is None]
    checks["web_dependencies"] = {
        "ok": not missing_web,
        "required": config.web.enabled,
        "missing": missing_web,
        "message": None if not missing_web else "缺少 Web 依赖；请运行 uv sync",
    }
    port_available = False
    already_running = False
    try:
        with socket.create_connection((config.web.host, config.web.port), timeout=0.15):
            already_running = True
    except OSError:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((config.web.host, config.web.port))
                port_available = True
        except OSError:
            port_available = False
    web_plist = Path.home() / "Library" / "LaunchAgents" / "com.local.douyin-wiki.web.plist"
    checks["web"] = {
        "ok": bool(config.web.enabled and (port_available or already_running)),
        "enabled": config.web.enabled,
        "address": f"http://{config.web.host}:{config.web.port}",
        "port_available": port_available,
        "running": already_running,
        "launch_agent_installed": web_plist.exists(),
        "message": (
            "Web 服务正在运行"
            if already_running
            else "端口可用，可运行 douyin-wiki web run"
            if port_available
            else "Web 端口已被其他程序占用"
        ),
    }
    browser_name = config.media.browser.lower()
    app_names = {
        "chrome": "Google Chrome.app",
        "safari": "Safari.app",
        "firefox": "Firefox.app",
        "edge": "Microsoft Edge.app",
        "arc": "Arc.app",
    }
    browser_app = Path("/Applications") / app_names.get(browser_name, f"{browser_name}.app")
    checks["browser"] = {"ok": browser_app.exists(), "path": str(browser_app)}
    profile = config.browser_profile_dir
    checks["douyin_browser_profile"] = {
        "ok": profile.exists(),
        "required": False,
        "path": str(profile),
        "message": (
            "图文与博主清点专用浏览器目录存在；这不代表登录仍有效，请运行 douyin-wiki auth status"
            if profile.exists()
            else "首次保存图文或清点博主前可运行 douyin-wiki auth douyin"
        ),
    }
    checks["vault"] = {"ok": config.vault_path.exists(), "path": str(config.vault_path)}
    checks["obsidian_vault"] = obsidian_vault_status(config.vault_path)
    checks["database"] = {
        "ok": config.database_path.exists(),
        "path": str(config.database_path),
    }
    api_key = get_secret(config.llm.api_key_env)
    backend_llm_required = config.analysis_mode == AnalysisMode.PROVIDER
    api_key_required = llm_api_key_required(config.llm.base_url)
    backend_llm_configured = llm_is_configured(config.llm, api_key)
    checks["llm"] = {
        "ok": backend_llm_configured or not backend_llm_required,
        "required": backend_llm_required,
        "model": config.llm.model,
        "base_url": config.llm.base_url,
        "api_key_env": config.llm.api_key_env,
        "api_key_required": api_key_required,
        "message": (
            None
            if backend_llm_configured
            else (
                "当前模式不需要后台模型；AI token 由 Gateway Agent 使用其已配置模型消耗"
                if config.analysis_mode == AnalysisMode.GATEWAY
                else "当前模式不需要后台模型"
                if config.analysis_mode == AnalysisMode.LOCAL
                else "provider 模式需要配置模型与 API key"
            )
        ),
    }
    checks["web_llm"] = {
        "ok": backend_llm_configured or not config.web.enabled,
        "required": config.web.enabled,
        "model": config.llm.model or "未配置",
        "message": (
            None
            if backend_llm_configured
            else (
                "文章浏览可正常使用；AI 对话请打开网页“模型设置”，"
                "或运行 douyin-wiki configure-model"
            )
        ),
    }
    sentence_transformers = importlib.util.find_spec("sentence_transformers") is not None
    checks["embeddings"] = {
        "ok": sentence_transformers,
        "model": config.embeddings.model,
        "message": None
        if sentence_transformers
        else "未安装 embeddings extra；将使用字符 n-gram fallback",
    }
    checks["mlx_whisper"] = {
        "ok": importlib.util.find_spec("mlx_whisper") is not None,
        "message": "未安装时自动使用 whisper CLI",
    }
    checks["transcription"] = {
        "ok": checks["mlx_whisper"]["ok"] or checks["whisper"]["ok"],
        "message": (
            None
            if checks["mlx_whisper"]["ok"] or checks["whisper"]["ok"]
            else "既未安装 MLX Whisper，也找不到 Whisper CLI；视频无法转录"
        ),
    }
    checks["ocr"] = {
        "ok": checks["swift"]["ok"],
        "message": None if checks["swift"]["ok"] else "缺少 Swift；视频和图文 OCR 不可用",
    }
    checks["reminders"] = {
        "ok": checks["osascript"]["ok"],
        "required": False,
        "message": (
            "命令可用；首次创建提醒时仍可能需要授予自动化权限"
            if checks["osascript"]["ok"]
            else "无法创建 macOS 提醒事项"
        ),
    }
    checks["overall"] = all(
        value.get("ok", False)
        for key, value in checks.items()
        if key
        in {
            "yt-dlp",
            "ffmpeg",
            "ffprobe",
            "git",
            "browser",
            "playwright",
            "transcription",
            "ocr",
            "vault",
            "obsidian_vault",
            "database",
            "llm",
            "web_dependencies",
            "web",
        }
    )
    return checks


class LaunchAgentInstaller:
    WORKER_LABEL = "com.local.douyin-wiki.worker"
    MAINTENANCE_LABEL = "com.local.douyin-wiki.maintenance"

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or default_config_path()
        self.launch_agents = Path.home() / "Library" / "LaunchAgents"
        self.logs = Path.home() / "Library" / "Logs" / "douyin-wiki"

    def install(self) -> list[Path]:
        self.launch_agents.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        worker_path = self.launch_agents / f"{self.WORKER_LABEL}.plist"
        maintenance_path = self.launch_agents / f"{self.MAINTENANCE_LABEL}.plist"
        executable_dirs = [
            str(Path(sys.executable).parent),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        inherited_path = os.environ.get("PATH", "").split(os.pathsep)
        launch_path = os.pathsep.join(dict.fromkeys([*executable_dirs, *inherited_path]))
        common_env = {
            "DOUYIN_WIKI_CONFIG": str(self.config_path),
            "PATH": launch_path,
        }
        worker = {
            "Label": self.WORKER_LABEL,
            "ProgramArguments": [
                sys.executable,
                "-m",
                "douyin_wiki",
                "worker",
                "run",
                "--forever",
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "EnvironmentVariables": common_env,
            "StandardOutPath": str(self.logs / "worker.log"),
            "StandardErrorPath": str(self.logs / "worker-error.log"),
        }
        maintenance = {
            "Label": self.MAINTENANCE_LABEL,
            "ProgramArguments": [
                sys.executable,
                "-m",
                "douyin_wiki",
                "maintenance",
                "run",
                "--apply",
            ],
            "StartCalendarInterval": {"Weekday": 0, "Hour": 3, "Minute": 0},
            "EnvironmentVariables": common_env,
            "StandardOutPath": str(self.logs / "maintenance.log"),
            "StandardErrorPath": str(self.logs / "maintenance-error.log"),
        }
        for path, payload in ((worker_path, worker), (maintenance_path, maintenance)):
            with path.open("wb") as handle:
                plistlib.dump(payload, handle)
            subprocess.run(["launchctl", "unload", str(path)], capture_output=True, check=False)
            subprocess.run(["launchctl", "load", str(path)], capture_output=True, check=False)
        return [worker_path, maintenance_path]

    def uninstall(self) -> list[Path]:
        removed: list[Path] = []
        for label in (self.WORKER_LABEL, self.MAINTENANCE_LABEL):
            path = self.launch_agents / f"{label}.plist"
            if path.exists():
                subprocess.run(["launchctl", "unload", str(path)], capture_output=True, check=False)
                path.unlink()
                removed.append(path)
        return removed


class WebLaunchAgentInstaller:
    LABEL = "com.local.douyin-wiki.web"

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or default_config_path()
        self.launch_agents = Path.home() / "Library" / "LaunchAgents"
        self.logs = Path.home() / "Library" / "Logs" / "douyin-wiki"

    @property
    def path(self) -> Path:
        return self.launch_agents / f"{self.LABEL}.plist"

    def install(self) -> Path:
        self.launch_agents.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        executable_dirs = [
            str(Path(sys.executable).parent),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        inherited_path = os.environ.get("PATH", "").split(os.pathsep)
        launch_path = os.pathsep.join(dict.fromkeys([*executable_dirs, *inherited_path]))
        payload = {
            "Label": self.LABEL,
            "ProgramArguments": [
                sys.executable,
                "-m",
                "douyin_wiki",
                "web",
                "run",
                "--config-path",
                str(self.config_path),
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "EnvironmentVariables": {
                "DOUYIN_WIKI_CONFIG": str(self.config_path),
                "PATH": launch_path,
            },
            "StandardOutPath": str(self.logs / "web.log"),
            "StandardErrorPath": str(self.logs / "web-error.log"),
        }
        with self.path.open("wb") as handle:
            plistlib.dump(payload, handle)
        subprocess.run(["launchctl", "unload", str(self.path)], capture_output=True, check=False)
        subprocess.run(["launchctl", "load", str(self.path)], capture_output=True, check=False)
        return self.path

    def uninstall(self) -> Path | None:
        if not self.path.exists():
            return None
        subprocess.run(["launchctl", "unload", str(self.path)], capture_output=True, check=False)
        self.path.unlink()
        return self.path

    def status(self, config: AppConfig) -> dict[str, Any]:
        running = False
        try:
            with socket.create_connection((config.web.host, config.web.port), timeout=0.2):
                running = True
        except OSError:
            pass
        return {
            "installed": self.path.exists(),
            "running": running,
            "address": f"http://{config.web.host}:{config.web.port}",
            "launch_agent": str(self.path),
            "log": str(self.logs / "web.log"),
            "error_log": str(self.logs / "web-error.log"),
        }
