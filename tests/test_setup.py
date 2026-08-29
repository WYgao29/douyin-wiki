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
    update_config_values,
    validate_vault_target,
    write_config,
)
from douyin_wiki.vault import VaultWriter

runner = CliRunner()


def test_gateway_is_default_analysis_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(render_default_config(AppConfig()), encoding="utf-8")
    assert load_config(config_path).analysis_mode == AnalysisMode.GATEWAY
    assert 'analysis_mode = "gateway"' in config_path.read_text(encoding="utf-8")
    assert load_config(tmp_path / "missing.toml").vault_path.name == "抖库"


def test_config_round_trips_quoted_strings_and_is_written_atomically(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    vault = tmp_path / '包含"引号的 Vault'
    config = AppConfig(
        vault_path=vault,
        llm={"model": 'provider/model"quoted'},
        media={"browser_profile": 'Profile "Work"'},
    )

    write_config(config, config_path, overwrite=True)
    loaded = load_config(config_path)

    assert loaded.vault_path == vault
    assert loaded.llm.model == 'provider/model"quoted'
    assert loaded.media.browser_profile == 'Profile "Work"'
    assert not list(tmp_path.glob(".config.toml.*.tmp"))


def test_config_patch_preserves_comments_and_unknown_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '# 用户注释\nanalysis_mode = "gateway"\ncustom_root = "keep"\n\n'
        '[llm]\nmodel = "old" # 模型注释\ncustom_llm = 42\n',
        encoding="utf-8",
    )

    update_config_values(
        config_path,
        {None: {"analysis_mode": "provider"}, "llm": {"model": "new"}},
    )

    content = config_path.read_text(encoding="utf-8")
    assert "# 用户注释" in content
    assert 'custom_root = "keep"' in content
    assert "custom_llm = 42" in content
    assert 'model = "new" # 模型注释' in content


def test_configure_model_allows_loopback_endpoint_without_api_key(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.toml"

    class FakeInstaller:
        WORKER_LABEL = "worker"

        def __init__(self, _: Path) -> None:
            self.launch_agents = tmp_path / "launch-agents"

        def install(self):
            raise AssertionError("worker should not be reinstalled in this test")

    monkeypatch.setattr("douyin_wiki.cli.LaunchAgentInstaller", FakeInstaller)
    monkeypatch.setattr(
        "douyin_wiki.cli.store_secret",
        lambda *_: (_ for _ in ()).throw(AssertionError("local endpoint must not store a key")),
    )

    result = runner.invoke(
        app,
        [
            "configure-model",
            "--model",
            "local-model",
            "--base-url",
            "http://127.0.0.1:1234/v1",
            "--config-path",
            str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "本机接口无需密钥" in result.output
    configured = load_config(config_path)
    assert configured.analysis_mode == AnalysisMode.PROVIDER
    assert configured.llm.model == "local-model"


def test_configure_model_does_not_expose_api_key_option() -> None:
    result = runner.invoke(app, ["configure-model", "--help"])

    assert result.exit_code == 0, result.output
    assert "--api-key" not in result.output


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
    assert "raw/assets/**/original.info.json" in gitignore
    assert "creators/**/raw/assets/**/original.info.json" in gitignore
    assert "creators/**/raw/avatar.*" in gitignore
    assert ".obsidian/" in gitignore
    assert "# 抖库" in (vault / "index.md").read_text(encoding="utf-8")
    assert "# 抖库维护规则" in (vault / "AGENTS.md").read_text(encoding="utf-8")
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


def test_existing_vault_receives_new_media_ignore_rules(tmp_path: Path) -> None:
    vault = tmp_path / "Existing"
    vault.mkdir()
    gitignore = vault / ".gitignore"
    gitignore.write_text(".douyin-wiki/\n", encoding="utf-8")

    changed = VaultWriter(vault).initialize(initialize_git=False)

    content = gitignore.read_text(encoding="utf-8")
    assert "creators/**/raw/assets/**/original.info.json" in content
    assert "creators/**/raw/covers/" in content
    assert "creators/**/raw/avatar.*" in content
    assert gitignore in changed


def test_existing_vault_migrates_only_legacy_brand_strings(tmp_path: Path) -> None:
    vault = tmp_path / "Existing"
    machine = vault / "wiki" / ".data" / "sources" / "123.md"
    machine.parent.mkdir(parents=True)
    (vault / "index.md").write_text("# 抖音知识库\n\n用户内容\n", encoding="utf-8")
    (vault / "AGENTS.md").write_text(
        "# Douyin Wiki 维护规则\n\n"
        "这个 Vault 是由 AI 维护、供 AI 检索的个人抖音知识库。\n"
        "用户追加规则\n",
        encoding="utf-8",
    )
    machine.write_text("此文件由 Douyin Wiki 管理，供 Agent 读取。\n", encoding="utf-8")

    changed = VaultWriter(vault).migrate_branding()

    assert "# 抖库" in (vault / "index.md").read_text(encoding="utf-8")
    agents = (vault / "AGENTS.md").read_text(encoding="utf-8")
    assert "# 抖库维护规则" in agents
    assert "用户追加规则" in agents
    assert "此文件由抖库管理" in machine.read_text(encoding="utf-8")
    assert set(changed) == {vault / "index.md", vault / "AGENTS.md", machine}


def test_existing_vault_migrates_visible_statuses_to_chinese(tmp_path: Path) -> None:
    vault = tmp_path / "Existing"
    source = vault / "wiki" / "sources" / "source.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\nstatus: active\nmedia_status: present\nmedia_retention: temporary\n---\n"
        "\n正文中的 active 不应被替换。\n",
        encoding="utf-8",
    )

    changed = VaultWriter(vault).migrate_visible_status_labels()

    content = source.read_text(encoding="utf-8")
    assert "status: 正常" in content
    assert "media_status: 已保留" in content
    assert "media_retention: 临时保留" in content
    assert "正文中的 active 不应被替换" in content
    assert changed == [source]


def test_existing_vault_migrates_visible_times_to_beijing_without_touching_inspiration(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "Existing"
    index = vault / "creators" / "示例_abcd" / "index.md"
    index.parent.mkdir(parents=True)
    index.write_text(
        "---\nlast_synced_at: 2026-08-23 08:40:41.757174+00:00\n---\n\n"
        "# 示例\n\n"
        "- 最近同步：2026-08-23T08:40:41.757174+00:00\n"
        "- 灵感原文：保留 2026-08-23T08:40:41.757174+00:00\n",
        encoding="utf-8",
    )

    changed = VaultWriter(vault).migrate_visible_times_to_beijing()

    content = index.read_text(encoding="utf-8")
    assert "last_synced_at: '2026-08-23T16:40:41.757174+08:00'" in content
    assert "最近同步：2026-08-23 16:40:41（北京时间）" in content
    assert "灵感原文：保留 2026-08-23T08:40:41.757174+00:00" in content
    assert changed == [index]


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
