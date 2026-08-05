from __future__ import annotations

import json
from hashlib import sha256

import pytest

from website_analytics.cache import write_audit_manifest, write_cached_json


_WINDOWS_DOS_DEVICE_NAMES = (
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
)
_WINDOWS_SUPERSCRIPT_DOS_DEVICE_NAMES = (
    "COM¹",
    "COM²",
    "COM³",
    "LPT¹",
    "LPT²",
    "LPT³",
)
_WINDOWS_COLLIDING_COMPONENTS = (
    "demo.",
    "demo ",
    "...",
    "nUl",
    "cOn.cache",
    "com¹",
    "lPt³.cache",
    *_WINDOWS_DOS_DEVICE_NAMES,
    *(f"{name}.cache" for name in _WINDOWS_DOS_DEVICE_NAMES),
    *_WINDOWS_SUPERSCRIPT_DOS_DEVICE_NAMES,
    *(f"{name}.cache" for name in _WINDOWS_SUPERSCRIPT_DOS_DEVICE_NAMES),
)


def test_cache_request_hash_is_deterministic_and_redacts_secret_values(tmp_path) -> None:
    first_request = {
        "property": "123",
        "Authorization": "Bearer first-value",
        "nested": {"api_key": "first-key"},
    }
    second_request = {
        "property": "123",
        "Authorization": "Bearer second-value",
        "nested": {"api_key": "second-key"},
    }
    rows = [{"metric": "sessions", "value": 42}]

    first_path = write_cached_json(tmp_path, "demo", "ga4", first_request, rows)
    second_path = write_cached_json(tmp_path, "demo", "ga4", second_request, rows)
    changed_path = write_cached_json(
        tmp_path,
        "demo",
        "ga4",
        {**second_request, "property": "456"},
        rows,
    )

    assert first_path == tmp_path / "demo" / "ga4" / first_path.name
    assert first_path == second_path
    assert first_path != changed_path
    assert json.loads(first_path.read_text(encoding="utf-8")) == rows
    assert first_request["Authorization"] == "Bearer first-value"
    assert first_request["nested"]["api_key"] == "first-key"


@pytest.mark.parametrize(
    ("site_key", "source"),
    [
        (".", "ga4"),
        ("..", "ga4"),
        ("demo/site", "ga4"),
        ("demo", "gsc\\nested"),
        ("C:", "ga4"),
        ("demo", "C:"),
    ],
)
def test_cache_rejects_unsafe_site_and_source_path_components(
    tmp_path, site_key: str, source: str
) -> None:
    with pytest.raises(ValueError, match="safe single path component"):
        write_cached_json(tmp_path, site_key, source, {"property": "123"}, [])

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("site_key", "source"),
    [
        *((component, "ga4") for component in _WINDOWS_COLLIDING_COMPONENTS),
        *(("demo", component) for component in _WINDOWS_COLLIDING_COMPONENTS),
    ],
)
def test_cache_rejects_windows_normalized_or_reserved_components_before_writes(
    tmp_path, site_key: str, source: str
) -> None:
    with pytest.raises(ValueError, match="safe single path component"):
        write_cached_json(tmp_path, site_key, source, {"property": "123"}, [])

    assert list(tmp_path.iterdir()) == []


def test_audit_manifest_redacts_nested_secrets_and_hashes_output(tmp_path) -> None:
    output_path = tmp_path / "report.json"
    output_path.write_bytes(b'{"summary":"synthetic"}\n')
    manifest_path = tmp_path / "audit.json"

    written_path = write_audit_manifest(
        manifest_path,
        {
            "property": "123",
            "nested": {"client_secret": "must-not-be-written"},
        },
        {"ga4": {"status": "ok", "row_count": 2}},
        output_path=output_path,
    )

    manifest_text = written_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert written_path == manifest_path
    assert manifest["request"]["nested"]["client_secret"] == "[REDACTED]"
    assert "must-not-be-written" not in manifest_text
    assert manifest["generated_at"].endswith("Z")
    assert manifest["source_statuses"] == {"ga4": {"status": "ok", "row_count": 2}}
    assert manifest["output_sha256"] == sha256(output_path.read_bytes()).hexdigest()
    assert manifest["package_version"] == "0.1.0"


def test_audit_manifest_rejects_a_missing_output_path(tmp_path) -> None:
    manifest_path = tmp_path / "audit.json"

    with pytest.raises(FileNotFoundError, match="output path does not exist"):
        write_audit_manifest(
            manifest_path,
            {"property": "123"},
            {"ga4": {"status": "ok", "row_count": 2}},
            output_path=tmp_path / "missing.xlsx",
        )

    assert not manifest_path.exists()
