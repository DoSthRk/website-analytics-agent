from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from website_analytics import __version__
from website_analytics.url_safety import (
    REDACTION_MARKER,
    is_sensitive_name,
    sanitize_url_query,
)


_UNSAFE_COMPONENT_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_DOS_NAMES = frozenset(
    (
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    )
)


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
        json.dumps(_redact(rows), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
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
            key: REDACTION_MARKER if _is_secret_key(key) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return sanitize_url_query(value)
    return value


def _is_secret_key(key: object) -> bool:
    return is_sensitive_name(key)


def _validate_component(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or value.endswith((".", " "))
        or _is_windows_reserved_component(value)
        or any(
            character in _UNSAFE_COMPONENT_CHARACTERS or ord(character) < 32
            for character in value
        )
    ):
        raise ValueError(f"{label} must be a safe single path component")


def _is_windows_reserved_component(value: str) -> bool:
    device_name = value.split(".", maxsplit=1)[0].rstrip(" ").casefold()
    return device_name in _WINDOWS_RESERVED_DOS_NAMES


def _path_within_root(candidate: Path, root: Path) -> Path:
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("cache path must stay within root") from error
    return resolved_candidate
