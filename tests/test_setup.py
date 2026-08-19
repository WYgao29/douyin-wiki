from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from douyin_wiki.cli import app
from douyin_wiki.config import AppConfig, load_config, render_default_config
from douyin_wiki.models import AnalysisMode
from douyin_wiki.setup import (
    VaultSetupMode,
    doctor,
    obsidian_vault_status,
    validate_vault_target,
)

runner = CliRunner()


def test_gateway_is_default_analysis_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(render_default_config(AppConfig()), encoding="utf-8")
    assert load_config(config_path).analysis_mode == AnalysisMode.GATEWAY
    assert 'analysis_mode = "gateway"' in config_path.read_text(encoding="utf-8")


def test_first_init_guides_new_vault_and_is_idempotent(tmp_path: Path) -> None:
    parent = tmp_path / "Obsidian"
    config_path = tmp_path / "config.toml"
    result = runner.invoke(
        app,
        ["init", "--config-path", str(config_path)],
        input=f"1\n{parent}\nMy-Douyin-Wiki\n",
    )
    assert result.exit_code == 0, result.output

    vault = parent / "My-Douyin-Wiki"
    assert (vault / ".obsidian").is_dir()
    assert (vault / ".douyin-wiki" / "state.sqlite3").is_file()
    assert (vault / "raw" / "assets").is_dir()
    assert (vault / "raw" / "covers").is_dir()
    assert (vault / "wiki" / "sources").is_dir()
    assert (vault / "wiki" / "concepts").is_dir()
    assert (vault / "wiki" / "entities").is_dir()
    assert (vault / "wiki" / "syntheses").is_dir()
    assert (vault / "index.md").is_file()
    assert (vault / "log.md").is_file()
    assert (vault / "AGENTS.md").is_file()
    assert (vault / ".git").is_dir()
    gitignore = (vault / ".gitignore").read_text(encoding="utf-8")
    assert "raw/covers/" in gitignore
    assert "raw/assets/**/cover.*" in gitignore
    assert ".obsidian/" in gitignore
    assert "打开 Obsidian" in result.output
    assert f'vault_path = "{vault}"' in config_path.read_text(encoding="utf-8")

    custom = vault / "AGENTS.md"
    custom.write_text("用户自定义内容\n", encoding="utf-8")
    repeated = runner.invoke(app, ["init", "--config-path", str(config_path)])
    assert repeated.exit_code == 0, repeated.output
    assert custom.read_text(encoding="utf-8") == "用户自定义内容\n"


def test_init_can_inject_into_existing_vault_without_overwrite(tmp_path: Path) -> None:
    vault = tmp_path / "Existing"
    vault.mkdir()
    note = vault / "我的笔记.md"
    note.write_text("保留", encoding="utf-8")
    config_path = tmp_path / "config.toml"

    result = runner.invoke(
        app,
        ["init", "--config-path", str(config_path)],
        input=f"2\n{vault}\n",
    )
    assert result.exit_code == 0, result.output
    assert note.read_text(encoding="utf-8") == "保留"
    assert (vault / "wiki" / "sources").is_dir()
    assert (vault / ".obsidian").is_dir()


def test_new_vault_rejects_nonempty_unmanaged_directory(tmp_path: Path) -> None:
    target = tmp_path / "notes"
    target.mkdir()
    (target / "existing.md").write_text("do not touch", encoding="utf-8")
    with pytest.raises(ValueError, match="使用已有 Vault"):
        validate_vault_target(target, VaultSetupMode.NEW)


def test_obsidian_registration_status(tmp_path: Path) -> None:
    vault = tmp_path / "Douyin-Wiki"
    vault.mkdir()
    registry = tmp_path / "obsidian.json"
    registry.write_text(
        json.dumps({"vaults": {"vault-id": {"path": str(vault), "open": True}}}),
        encoding="utf-8",
    )
    registered = obsidian_vault_status(vault, registry_path=registry)
    assert registered["ok"] is True
    assert registered["action"] is None

    missing = obsidian_vault_status(tmp_path / "Other", registry_path=registry)
    assert missing["ok"] is False
    assert "打开本地文件夹作为仓库" in missing["action"]


def test_doctor_reports_unregistered_obsidian_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "Douyin-Wiki"
    vault.mkdir()
    registry = tmp_path / "obsidian.json"
    registry.write_text('{"vaults": {}}', encoding="utf-8")
    monkeypatch.setattr("douyin_wiki.setup.default_obsidian_registry_path", lambda: registry)

    checks = doctor(AppConfig(vault_path=vault))
    assert checks["obsidian_vault"]["ok"] is False
    assert str(vault) in checks["obsidian_vault"]["action"]
