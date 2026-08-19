#!/usr/bin/env python3
"""Hermes monitor source: silent when no durable Douyin Wiki events exist."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def main() -> None:
    configured = os.environ.get("DOUYIN_WIKI_PROJECT")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path.cwd(),
        Path(__file__).resolve().parents[1],
    ]
    project = next(
        (
            candidate
            for candidate in candidates
            if candidate is not None and (candidate / "pyproject.toml").is_file()
        ),
        None,
    )
    if project is None:
        raise SystemExit("Douyin Wiki project not found; set DOUYIN_WIKI_PROJECT")
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    environment = dict(os.environ)
    # Hermes may run the monitor from its own virtualenv. Let uv select this
    # project's locked environment instead of accidentally reusing that one.
    for variable in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT"):
        environment.pop(variable, None)
    os.execve(
        uv,
        [
            uv,
            "--directory",
            str(project),
            "run",
            "douyin-wiki",
            "gateway",
            "monitor-events",
        ],
        environment,
    )


if __name__ == "__main__":
    main()
