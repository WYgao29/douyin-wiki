from __future__ import annotations

import asyncio
import importlib.metadata
import plistlib
from pathlib import Path

import douyin_wiki
from douyin_wiki.mcp_server import mcp
from douyin_wiki.secrets import get_secret
from douyin_wiki.setup import LaunchAgentInstaller


def test_package_version_matches_metadata() -> None:
    assert douyin_wiki.__version__ == importlib.metadata.version("douyin-wiki")


def test_mcp_exposes_public_tools() -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {
        "capture_douyin",
        "get_job",
        "list_jobs",
        "get_auth_status",
        "list_job_events",
        "acknowledge_job_event",
        "get_analysis_context",
        "submit_transcript_correction",
        "submit_gateway_analysis",
        "approve_job",
        "retry_job",
        "resolve_review",
        "submit_analysis",
        "reanalyze_entry",
        "reanalyze_all",
        "search_knowledge",
        "create_topic",
        "get_topic",
        "list_topics",
        "set_topic_sources",
        "search_topic",
        "generate_topic_artifact",
        "save_topic_note",
        "get_entry",
        "add_inspiration",
        "add_purpose",
        "confirm_reminder",
        "run_maintenance",
        "doctor",
    } <= names
    assert "submit_fact_checks" not in names


def test_secret_prefers_environment(monkeypatch) -> None:
    monkeypatch.setenv("DOUYIN_WIKI_TEST_KEY", "secret-value")
    assert get_secret("DOUYIN_WIKI_TEST_KEY") == "secret-value"


def test_launch_agent_has_homebrew_path(monkeypatch, tmp_path: Path) -> None:
    installer = LaunchAgentInstaller(tmp_path / "config.toml")
    installer.launch_agents = tmp_path / "LaunchAgents"
    installer.logs = tmp_path / "Logs"
    monkeypatch.setattr("douyin_wiki.setup.subprocess.run", lambda *args, **kwargs: None)
    worker_path, _ = installer.install()
    with worker_path.open("rb") as handle:
        payload = plistlib.load(handle)
    assert "/opt/homebrew/bin" in payload["EnvironmentVariables"]["PATH"].split(":")
