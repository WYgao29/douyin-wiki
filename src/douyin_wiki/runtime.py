from __future__ import annotations

import hashlib
from pathlib import Path


def source_signature(package_root: Path | None = None) -> str:
    """Return a stable fingerprint of the currently installed Python sources."""
    root = (package_root or Path(__file__).resolve().parent).resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root)
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


LOADED_SOURCE_SIGNATURE = source_signature()
