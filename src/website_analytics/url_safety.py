"""Redact sensitive query-parameter values before data leaves memory."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REDACTION_MARKER = "[REDACTED]"
_SENSITIVE_PARAMETER_NAMES = frozenset(
    {
        "token",
        "accesstoken",
        "refreshtoken",
        "apikey",
        "xgoogapikey",
        "key",
        "password",
        "secret",
        "code",
        "signature",
    }
)


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
    if not any(_is_sensitive_parameter(name) for name, _ in parameters):
        return value
    safe_query = urlencode(
        [
            (name, REDACTION_MARKER if _is_sensitive_parameter(name) else parameter_value)
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


def _is_sensitive_parameter(name: str) -> bool:
    normalized = "".join(character for character in name.casefold() if character.isalnum())
    return normalized in _SENSITIVE_PARAMETER_NAMES
