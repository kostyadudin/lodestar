"""Utility helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def token_estimate(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def ensure_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
