from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_gateway_docs_use_available_cli_monitor_command() -> None:
    docs = (REPOSITORY_ROOT / "docs" / "gateway-agents.md").read_text(encoding="utf-8")

    assert "scripts/hermes_event_monitor.py" not in docs
    assert "douyin-wiki gateway monitor-events" in docs
