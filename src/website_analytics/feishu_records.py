"""Idempotent aggregate-record synchronization through the approved Lark CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class FeishuTarget:
    base_token: str
    overview_table: str
    product_table: str
    identity: str


class RecordClient(Protocol):
    def list_records(self, table_id: str) -> list[tuple[str, dict[str, Any]]]: ...

    def create_records(self, table_id: str, records: Sequence[Mapping[str, Any]]) -> int: ...

    def update_record(self, table_id: str, record_id: str, fields: Mapping[str, Any]) -> None: ...


_TRANSIENT_LARK_ERROR_MARKERS = (
    "eof",
    "connection reset",
    "connection refused",
    "i/o timeout",
    "tls handshake timeout",
    "context deadline exceeded",
    "timed out",
    "timeout",
    "too many requests",
    "rate limit",
    "onoverqpslimit",
    "cluster_limits",
    "http 429",
    "status 429",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "status 500",
    "status 502",
    "status 503",
    "status 504",
)


def is_retryable_lark_failure(*, code: Any, message: Any, raw: str) -> bool:
    """Return whether a failed lark-cli call is safe to retry."""

    if code == 800004135:
        return True
    details = " ".join(str(value) for value in (message, raw) if value).lower()
    return any(marker in details for marker in _TRANSIENT_LARK_ERROR_MARKERS)


def lark_cli_environment() -> dict[str, str]:
    """Build the lark-cli subprocess environment with an optional dedicated proxy."""

    environment = dict(os.environ)
    environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    proxy_mapping = {
        "LARK_CLI_HTTP_PROXY": ("HTTP_PROXY", "http_proxy"),
        "LARK_CLI_HTTPS_PROXY": ("HTTPS_PROXY", "https_proxy"),
    }
    for source, targets in proxy_mapping.items():
        proxy = environment.get(source)
        if proxy:
            for target in targets:
                environment[target] = proxy
    return environment


def load_feishu_target(path: Path) -> FeishuTarget:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Feishu sync target is unreadable or invalid JSON") from error
    if not isinstance(document, Mapping):
        raise ValueError("Feishu sync target must contain an object")
    allowed = {"base_token", "overview_table", "product_table", "identity"}
    if set(document) != allowed:
        raise ValueError("Feishu sync target has unexpected fields")
    identity = _text(document, "identity")
    if identity not in {"user", "bot"}:
        raise ValueError("Feishu identity must be user or bot")
    return FeishuTarget(
        base_token=_text(document, "base_token"),
        overview_table=_text(document, "overview_table"),
        product_table=_text(document, "product_table"),
        identity=identity,
    )


def sync_record_sets(
    client: RecordClient,
    target: FeishuTarget,
    overview: Sequence[Mapping[str, Any]],
    products: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Create missing keys and update existing keys without deleting any row."""
    overview_result = _sync_table(
        client,
        target.overview_table,
        "周期键",
        overview,
    )
    product_result = _sync_table(
        client,
        target.product_table,
        "产品周期键",
        products,
    )
    return {
        "overview_created": overview_result["created"],
        "overview_updated": overview_result["updated"],
        "product_created": product_result["created"],
        "product_updated": product_result["updated"],
    }


def _sync_table(
    client: RecordClient,
    table_id: str,
    key_field: str,
    desired: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    desired_by_key = _unique_desired(desired, key_field)
    existing_by_key: dict[str, str] = {}
    for record_id, fields in client.list_records(table_id):
        value = fields.get(key_field)
        if value is None:
            continue
        key = _single_text(value, key_field)
        if key in existing_by_key:
            raise ValueError(f"Feishu table contains duplicate {key_field} values")
        existing_by_key[key] = record_id

    creates = [record for key, record in desired_by_key.items() if key not in existing_by_key]
    updates = [
        (existing_by_key[key], record)
        for key, record in desired_by_key.items()
        if key in existing_by_key
    ]
    created = client.create_records(table_id, creates) if creates else 0
    for record_id, fields in updates:
        client.update_record(table_id, record_id, fields)
    return {"created": created, "updated": len(updates)}


def _unique_desired(
    records: Sequence[Mapping[str, Any]], key_field: str
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for fields in records:
        key = _single_text(fields.get(key_field), key_field)
        if key in result:
            raise ValueError(f"desired records contain duplicate {key_field} values")
        result[key] = fields
    return result


class LarkCLIRecordClient:
    def __init__(
        self,
        target: FeishuTarget,
        *,
        runner: Callable[[list[str]], dict[str, Any]] | None = None,
        update_delay_seconds: float = 1.1,
    ) -> None:
        self._target = target
        self._runtime = _runtime()
        self._runner = runner or self._run
        self._update_delay_seconds = update_delay_seconds

    def list_records(self, table_id: str) -> list[tuple[str, dict[str, Any]]]:
        records: list[tuple[str, dict[str, Any]]] = []
        offset = 0
        while True:
            result = self._runner(
                [
                    *self._runtime,
                    "base",
                    "+record-list",
                    *self._common(),
                    "--table-id",
                    table_id,
                    "--offset",
                    str(offset),
                    "--limit",
                    "200",
                    "--format",
                    "json",
                ]
            )
            data = result.get("data")
            if not isinstance(data, Mapping):
                raise RuntimeError("Feishu record list has no data")
            fields = data.get("fields")
            rows = data.get("data")
            record_ids = data.get("record_id_list")
            if not isinstance(fields, list) or not all(isinstance(value, str) for value in fields):
                raise RuntimeError("Feishu record list has invalid fields")
            if not isinstance(rows, list) or not isinstance(record_ids, list) or len(rows) != len(record_ids):
                raise RuntimeError("Feishu record list has invalid rows")
            for record_id, row in zip(record_ids, rows, strict=True):
                if not isinstance(record_id, str) or not isinstance(row, list) or len(row) != len(fields):
                    raise RuntimeError("Feishu record list row is invalid")
                records.append((record_id, dict(zip(fields, row, strict=True))))
            has_more = data.get("has_more")
            if not isinstance(has_more, bool):
                raise RuntimeError("Feishu record list has invalid pagination state")
            if not has_more:
                break
            if not rows:
                raise RuntimeError("Feishu record list pagination made no progress")
            offset += len(rows)
        return records

    def create_records(self, table_id: str, records: Sequence[Mapping[str, Any]]) -> int:
        created = 0
        for start in range(0, len(records), 200):
            batch = list(records[start : start + 200])
            fields = list(batch[0])
            if any(set(record) != set(fields) for record in batch):
                raise ValueError("Feishu create batch records must share the same fields")
            payload = {"fields": fields, "rows": [[record[field] for field in fields] for record in batch]}
            result = self._runner(
                [
                    *self._runtime,
                    "base",
                    "+record-batch-create",
                    *self._common(),
                    "--table-id",
                    table_id,
                    "--json",
                    _json(payload),
                ]
            )
            data = result.get("data")
            ids = data.get("record_id_list") if isinstance(data, Mapping) else None
            if not isinstance(ids, list) or len(ids) != len(batch):
                raise RuntimeError("Feishu create response did not confirm every record")
            created += len(ids)
        return created

    def update_record(self, table_id: str, record_id: str, fields: Mapping[str, Any]) -> None:
        self._runner(
            [
                *self._runtime,
                "base",
                "+record-upsert",
                *self._common(),
                "--table-id",
                table_id,
                "--record-id",
                record_id,
                "--json",
                _json(fields),
            ]
        )
        if self._update_delay_seconds:
            time.sleep(self._update_delay_seconds)

    def _common(self) -> list[str]:
        return [
            "--as",
            self._target.identity,
            "--base-token",
            self._target.base_token,
        ]

    def _run(self, command: list[str]) -> dict[str, Any]:
        environment = lark_cli_environment()
        for attempt in range(6):
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                check=False,
            )
            raw = completed.stdout or completed.stderr
            try:
                result = json.loads(raw)
            except json.JSONDecodeError as error:
                if attempt < 5 and is_retryable_lark_failure(code=None, message=None, raw=raw):
                    time.sleep(min(3 * (attempt + 1), 15))
                    continue
                raise RuntimeError("lark-cli returned a non-JSON response") from error
            if not isinstance(result, dict):
                raise RuntimeError("lark-cli returned an invalid response")
            error_data = result.get("error")
            code = error_data.get("code") if isinstance(error_data, Mapping) else None
            message = error_data.get("message") if isinstance(error_data, Mapping) else None
            failed = completed.returncode != 0 or result.get("ok") is not True
            if failed and attempt < 5 and is_retryable_lark_failure(
                code=code,
                message=message,
                raw=raw,
            ):
                time.sleep(min(3 * (attempt + 1), 15))
                continue
            if failed:
                raise RuntimeError(f"lark-cli failed (code={code}, message={message or 'unknown'})")
            return result
        raise RuntimeError("lark-cli retry limit exceeded")


def _runtime() -> list[str]:
    wrapper = shutil.which("lark-cli")
    if wrapper is None:
        raise RuntimeError("lark-cli is unavailable")
    if os.name != "nt":
        return [wrapper]
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node is unavailable")
    script = Path(wrapper).parent / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
    if not script.is_file():
        raise RuntimeError("lark-cli runtime script is unavailable")
    return [node, str(script)]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _text(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _single_text(value: object, field: str) -> str:
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must contain one text value")
    return value
