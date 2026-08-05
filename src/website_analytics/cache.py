from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from website_analytics import __version__


_REDACTION_MARKER = "[REDACTED]"
_SECRET_KEY_PARTS = (
    "token",
    "secret",
    "credential",
    "authorization",
    "password",
    "api_key",
)
_UNSAFE_COMPONENT_CHARACTERS = frozenset('<>:"/\\|?*')


def write_cached_json(
    root: str | Path,
    site_key: str,
    source: str,
    request: Any,
    rows: Any,
) -> Path:
    """Write source response rows under a request-derived cache key."""
    _validate_component(site_key, "site key")
    _validate_component(source, "source")

    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    resolved_root = root_path.resolve()
    site_path = _path_within_root(resolved_root / site_key, resolved_root)
    site_path.mkdir(exist_ok=True)
    source_path = _path_within_root(site_path / source, resolved_root)
    source_path.mkdir(exist_ok=True)

    request_hash = hashlib.sha256(
        _canonical_json(_redact(request)).encode("utf-8")
    ).hexdigest()
    cache_path = _path_within_root(
        source_path / f"{request_hash}.json", resolved_root
    )
    cache_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return cache_path


def write_audit_manifest(
    path: str | Path,
    request: Any,
    source_statuses: Any,
    output_path: str | Path | None = None,
) -> Path:
    """Write a redacted local manifest describing a report-generation request."""
    output_hash = _hash_output(output_path) if output_path is not None else None
    manifest = {
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "package_version": __version__,
        "request": _redact(request),
        "source_statuses": _redact(source_statuses),
    }
    if output_hash is not None:
        manifest["output_sha256"] = output_hash

    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_output(output_path: str | Path) -> str:
    path = Path(output_path)
    if not path.exists():
        raise FileNotFoundError(f"output path does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"output path must be a file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _REDACTION_MARKER if _is_secret_key(key) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _is_secret_key(key: object) -> bool:
    return isinstance(key, str) and any(part in key.casefold() for part in _SECRET_KEY_PARTS)


def _validate_component(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or any(
            character in _UNSAFE_COMPONENT_CHARACTERS or ord(character) < 32
            for character in value
        )
    ):
        raise ValueError(f"{label} must be a safe single path component")


def _path_within_root(candidate: Path, root: Path) -> Path:
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("cache path must stay within root") from error
    return resolved_candidate
