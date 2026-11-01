"""Content-addressed JSON cache for completed M5 evaluation results."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any


CACHE_SCHEMA_VERSION = 1


def sha256_file(path: Path) -> str:
    """Hash a source file in bounded chunks so input size does not affect memory use."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def make_cache_key(configuration: dict[str, Any]) -> str:
    """Create a stable key from JSON-compatible data and evaluation parameters."""
    encoded = json.dumps(
        {"cache_schema_version": CACHE_SCHEMA_VERSION, "configuration": configuration},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def cache_path(cache_directory: Path, key: str) -> Path:
    if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
        raise ValueError("cache key must be a lowercase SHA-256 digest")
    return Path(cache_directory) / f"{key}.json"


def read_cached_result(cache_directory: Path, key: str) -> dict[str, Any] | None:
    """Read a valid completed result; corrupted or mismatched entries are cache misses."""
    path = cache_path(cache_directory, key)
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    result = entry.get("result") if isinstance(entry, dict) else None
    if (
        entry.get("cache_schema_version") != CACHE_SCHEMA_VERSION
        or entry.get("cache_key") != key
        or not isinstance(result, dict)
        or result.get("status") != "completed"
    ):
        return None
    return result


def write_cached_result(
    cache_directory: Path,
    key: str,
    result: dict[str, Any],
) -> Path:
    """Publish a completed result atomically, leaving no partial cache entry."""
    if result.get("status") != "completed":
        raise ValueError("only completed forecast runs can be cached")
    path = cache_path(cache_directory, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp")
    entry = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "cache_key": key,
        "result": result,
    }
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(entry, output, indent=2, ensure_ascii=False, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path
