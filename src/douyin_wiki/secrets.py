from __future__ import annotations

import hmac
import os
import subprocess

from .errors import ExternalToolError

KEYCHAIN_SERVICE = "douyin-wiki"


def _read_keychain_secret(account: str) -> str:
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
    return result.stdout.rstrip("\r\n") if result.returncode == 0 else ""


def get_secret(account: str) -> str:
    if value := os.environ.get(account):
        return value
    return _read_keychain_secret(account)


def store_secret(account: str, value: str) -> None:
    if not value:
        raise ValueError("secret cannot be empty")
    if "\n" in value or "\r" in value:
        raise ValueError("secret cannot contain line breaks")
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
            ],
            # With -w as the final option, macOS security prompts for the password
            # and its confirmation. Both lines are required on non-TTY stdin.
            input=f"{value}\n{value}\n",
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
    stored = _read_keychain_secret(account)
    if not stored or not hmac.compare_digest(stored, value):
        raise ExternalToolError("模型密钥写入 Keychain 后校验失败")
