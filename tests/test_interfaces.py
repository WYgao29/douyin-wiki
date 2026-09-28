from __future__ import annotations

import asyncio
import importlib.metadata
import json
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import douyin_wiki
from douyin_wiki import cli, mcp_server
from douyin_wiki.errors import ExternalToolError, JobStateError
from douyin_wiki.mcp_server import mcp
from douyin_wiki.models import AnalysisMode, GatewayContext, JobStatus
from douyin_wiki.secrets import get_secret, store_secret
from douyin_wiki.setup import LaunchAgentInstaller, WebLaunchAgentInstaller
from douyin_wiki.worker import Worker


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


def test_store_secret_confirms_stdin_and_verifies_keychain_write(monkeypatch) -> None:
    calls = []
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="secret-value\n", stderr=""),
        ]
    )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return next(responses)

    monkeypatch.setattr("douyin_wiki.secrets.subprocess.run", fake_run)
    store_secret("DOUYIN_WIKI_TEST_KEY", "secret-value")

    assert calls[0][0][-1] == "-w"
    assert calls[0][1]["input"] == "secret-value\nsecret-value\n"
    assert calls[1][0][1] == "find-generic-password"


def test_store_secret_rejects_failed_readback(monkeypatch) -> None:
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="\n", stderr=""),
        ]
    )
    monkeypatch.setattr(
        "douyin_wiki.secrets.subprocess.run", lambda *_, **__: next(responses)
    )

    with pytest.raises(ExternalToolError, match="写入 Keychain 后校验失败"):
        store_secret("DOUYIN_WIKI_TEST_KEY", "secret-value")


def test_mcp_preserves_structured_douyin_wiki_error(monkeypatch) -> None:
    class BrokenService:
        def get_job(self, job_id: str):
            raise JobStateError("任务状态不允许", details={"job_id": job_id})

    monkeypatch.setattr(mcp_server, "_SERVICE", BrokenService())
    with pytest.raises(ToolError) as captured:
        asyncio.run(mcp.call_tool("get_job", {"job_id": "job-bad"}))

    payload = json.loads(str(captured.value))
    assert payload == {
        "error": {
            "code": "invalid_job_state",
            "message": "任务状态不允许",
            "details": {"job_id": "job-bad"},
        }
    }


@pytest.mark.asyncio
async def test_mcp_gateway_video_order_validation_and_reanalysis(service, monkeypatch) -> None:
    service.config.analysis_mode = AnalysisMode.GATEWAY
    monkeypatch.setattr(mcp_server, "_SERVICE", service)
    job = service.capture_douyin(
        "https://v.douyin.com/uvHsRpXIn8s/",
        gateway_context=GatewayContext(gateway="test-agent", conversation_id="thread-1"),
    )
    paused = await Worker(service).run_once()
    assert paused.status == JobStatus.AWAITING_AGENT_ANALYSIS
    await mcp.call_tool("get_analysis_context", {"job_id": job.id})

    with pytest.raises(ToolError, match="请先提交逐字稿校正"):
        await mcp.call_tool(
            "submit_gateway_analysis",
            {"job_id": job.id, "analysis": {"title": "测试"}, "producer": "test-agent"},
        )
    for corrections in (
        [{"id": 0, "text": ""}],
        [{"id": 999, "text": "未知片段"}],
        [{"id": 0, "text": "正文"}, {"id": 0, "text": "重复正文"}],
    ):
        with pytest.raises(ToolError):
            await mcp.call_tool(
                "submit_transcript_correction",
                {"job_id": job.id, "corrections": corrections, "producer": "test-agent"},
            )
        assert "transcript_corrected" not in service.database.get_job(job.id).artifacts

    await mcp.call_tool(
        "submit_transcript_correction",
        {
            "job_id": job.id,
            "corrections": [{"id": 0, "text": "离职以后，我取关了很多财经媒体。"}],
            "producer": "test-agent",
        },
    )
    assert service.database.get_job(job.id).result["phase"] == "analysis"
    with pytest.raises(ToolError):
        await mcp.call_tool(
            "submit_gateway_analysis",
            {"job_id": job.id, "analysis": {"one_liner": "缺少标题"}, "producer": "test-agent"},
        )
    assert "analysis" not in service.database.get_job(job.id).artifacts
    await mcp.call_tool(
        "submit_gateway_analysis",
        {
            "job_id": job.id,
            "analysis": {"title": "测试资料", "one_liner": "测试摘要"},
            "producer": "test-agent",
        },
    )
    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED
    entry_id = completed.result["entry_id"]
    await mcp.call_tool(
        "reanalyze_entry",
        {
            "entry_id": entry_id,
            "force": True,
            "gateway_context": {"gateway": "test-agent", "conversation_id": "thread-1"},
        },
    )
    reanalysis = service.list_jobs()[0]
    resumed = await Worker(service).run_once()
    assert resumed.id == reanalysis.id
    assert resumed.status == JobStatus.AWAITING_AGENT_ANALYSIS
    assert service.get_analysis_context(resumed.id)["phase"] == "analysis"


@pytest.mark.asyncio
async def test_mcp_gateway_review_requires_resolution(service, monkeypatch) -> None:
    service.config.analysis_mode = AnalysisMode.GATEWAY
    monkeypatch.setattr(mcp_server, "_SERVICE", service)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    paused = await Worker(service).run_once()
    assert paused.status == JobStatus.AWAITING_AGENT_ANALYSIS
    await mcp.call_tool(
        "submit_transcript_correction",
        {
            "job_id": job.id,
            "corrections": [{"id": 0, "text": "离职以后，我取关了很多财经媒体。"}],
            "producer": "test-agent",
            "review_issues": [
                {
                    "id": "agent-0",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "raw_text": "离职以后我取关了很多财经媒体。",
                    "reason": "专有名词待核实",
                    "suggestions": ["离职以后，我取关了很多财经媒体。"],
                }
            ],
        },
    )
    assert service.database.get_job(job.id).status == JobStatus.NEEDS_REVIEW
    with pytest.raises(ToolError):
        await mcp.call_tool(
            "submit_gateway_analysis",
            {"job_id": job.id, "analysis": {"title": "测试"}, "producer": "test-agent"},
        )
    with pytest.raises(ToolError, match="必须解决全部疑点"):
        await mcp.call_tool("resolve_review", {"job_id": job.id, "resolutions": {}})
    assert service.database.get_job(job.id).status == JobStatus.NEEDS_REVIEW
    await mcp.call_tool(
        "resolve_review",
        {"job_id": job.id, "resolutions": {"agent-0": "离职以后，我取关了很多财经媒体。"}},
    )
    resumed = await Worker(service).run_once()
    assert resumed.status == JobStatus.AWAITING_AGENT_ANALYSIS
    assert service.get_analysis_context(job.id)["phase"] == "analysis"


@pytest.mark.asyncio
async def test_gateway_monitor_replays_unacked_and_detects_same_status_phase(
    service, monkeypatch, capsys
) -> None:
    service.config.analysis_mode = AnalysisMode.GATEWAY
    monkeypatch.setattr(cli, "_service", lambda _config_path: service)
    monkeypatch.setattr(mcp_server, "_SERVICE", service)
    job = service.capture_douyin(
        "https://v.douyin.com/uvHsRpXIn8s/",
        gateway_context=GatewayContext(gateway="test-agent", conversation_id="thread-1"),
    )
    cli.gateway_monitor_events()
    assert capsys.readouterr().out == ""
    await Worker(service).run_once()
    cli.gateway_monitor_events()
    first = capsys.readouterr().out
    assert len(json.loads(first)) == 1
    assert json.loads(first)[0]["result"]["phase"] == "transcript_correction"
    cli.gateway_monitor_events()
    assert capsys.readouterr().out == first
    restarted_store = type(service.database)(service.config.database_path)
    assert restarted_store.list_job_events()[0].id == json.loads(first)[0]["id"]

    await mcp.call_tool(
        "submit_transcript_correction",
        {
            "job_id": job.id,
            "corrections": [{"id": 0, "text": "离职以后，我取关了很多财经媒体。"}],
            "producer": "test-agent",
        },
    )
    cli.gateway_monitor_events()
    second = json.loads(capsys.readouterr().out)
    assert len(second) == 1
    assert second[0]["id"] != json.loads(first)[0]["id"]
    assert second[0]["status"] == json.loads(first)[0]["status"]
    assert second[0]["result"]["phase"] == "analysis"
    service.acknowledge_job_event(second[0]["id"])
    cli.gateway_monitor_events()
    assert capsys.readouterr().out == ""


def test_launch_agent_has_homebrew_path_and_unbuffered_logs(monkeypatch, tmp_path: Path) -> None:
    installer = LaunchAgentInstaller(tmp_path / "config.toml")
    installer.launch_agents = tmp_path / "LaunchAgents"
    installer.logs = tmp_path / "Logs"
    monkeypatch.setattr("douyin_wiki.setup.subprocess.run", lambda *args, **kwargs: None)
    worker_path, maintenance_path = installer.install()
    with worker_path.open("rb") as handle:
        payload = plistlib.load(handle)
    assert "/opt/homebrew/bin" in payload["EnvironmentVariables"]["PATH"].split(":")
    assert payload["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"
    with maintenance_path.open("rb") as handle:
        maintenance = plistlib.load(handle)
    assert maintenance["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"


def test_web_launch_agent_has_unbuffered_logs(monkeypatch, tmp_path: Path) -> None:
    installer = WebLaunchAgentInstaller(tmp_path / "config.toml")
    installer.launch_agents = tmp_path / "LaunchAgents"
    installer.logs = tmp_path / "Logs"
    monkeypatch.setattr("douyin_wiki.setup.subprocess.run", lambda *args, **kwargs: None)
    with installer.install().open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"
