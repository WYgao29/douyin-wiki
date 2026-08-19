from __future__ import annotations

import os
import subprocess

from .errors import ExternalToolError

KEYCHAIN_SERVICE = "douyin-wiki"


def get_secret(account: str) -> str:
    if value := os.environ.get(account):
        return value
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                account,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def store_secret(account: str, value: str) -> None:
    if not value:
        raise ValueError("secret cannot be empty")
    try:
        result = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                account,
                "-w",
                value,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ExternalToolError("无法访问 macOS Keychain") from exc
    if result.returncode != 0:
        raise ExternalToolError(
            "无法把模型密钥保存到 macOS Keychain",
            details={"stderr": result.stderr.strip()},
        )
