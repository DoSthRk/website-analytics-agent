"""Redact sensitive query-parameter values before data leaves memory."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REDACTION_MARKER = "[REDACTED]"
_SENSITIVE_NAME_PARTS = frozenset(
    {
        "token",
        "secret",
        "credential",
        "authorization",
        "password",
        "privatekey",
        "apikey",
    }
)
_SENSITIVE_EXACT_NAMES = frozenset({"key", "code", "signature"})


def sanitize_url_query(value: str) -> str:
    """Return ``value`` with sensitive URL query values replaced.

    Non-sensitive paths and query parameters are intentionally retained for
    diagnostics and attribution analysis. If no sensitive parameter is found,
    return the original string exactly instead of normalizing its encoding.
    """
    if "?" not in value:
        return value
    parts = urlsplit(value)
    parameters = parse_qsl(parts.query, keep_blank_values=True)
    if not any(is_sensitive_name(name) for name, _ in parameters):
        return value
    safe_query = urlencode(
        [
            (name, REDACTION_MARKER if is_sensitive_name(name) else parameter_value)
            for name, parameter_value in parameters
        ],
        doseq=True,
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, safe_query, parts.fragment))


def sanitize_url_values(value: Any) -> Any:
    """Recursively sanitize strings in JSON-like values without mutating input."""
    if isinstance(value, Mapping):
        return {key: sanitize_url_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_url_values(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_url_values(item) for item in value)
    if isinstance(value, str):
        return sanitize_url_query(value)
    return value


def is_sensitive_name(value: object) -> bool:
    """Apply one normalized policy to mapping keys and URL parameters."""
    if not isinstance(value, str):
        return False
    normalized = normalize_sensitive_name(value)
    return normalized in _SENSITIVE_EXACT_NAMES or any(
        part in normalized for part in _SENSITIVE_NAME_PARTS
    )


def normalize_sensitive_name(value: str) -> str:
    """Normalize case, camel case, hyphenated, and header-style names."""
    return "".join(character for character in value.casefold() if character.isalnum())
