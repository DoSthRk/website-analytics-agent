"""Fixed, read-only adapter for the legacy pages/urltable classification dimension."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from website_analytics.models import InquirySourceConfig
from website_analytics.windows_credentials import (
    WindowsCredentialError,
    load_windows_generic_credential,
)


_DIMENSION_QUERY = """
SELECT u.url AS route_url,
       u.pageid AS route_page_id,
       u.dbname AS route_source,
       p.pageid AS content_page_id,
       p.template AS template
FROM urltable AS u
LEFT JOIN pages AS p ON u.dbname = 'pages' AND p.pageid = u.pageid
ORDER BY u.url, u.dbname, u.pageid
"""


class PageDimensionSourceError(RuntimeError):
    """Raised when the approved read-only page dimension cannot be queried."""


class _Cursor(Protocol):
    def execute(self, query: str, args: Sequence[object] | None = None) -> object: ...
    def fetchall(self) -> Sequence[Mapping[str, object]]: ...


class _Connection(Protocol):
    def cursor(self) -> Any: ...
    def close(self) -> object: ...


Connector = Callable[..., _Connection]


class LegacyPageDimensionAdapter:
    """Read only route IDs and templates; never read page content or form PII."""

    def __init__(self, dsn: str, *, connector: Connector | None = None) -> None:
        self._connection_options = _connection_options(dsn)
        self._connector = connector or _pymysql_connect

    def query(self) -> list[dict[str, object]]:
        connection: _Connection | None = None
        try:
            connection = self._connector(**self._connection_options)
            with connection.cursor() as cursor:
                cursor.execute(_DIMENSION_QUERY)
                return [_dimension_row(row) for row in cursor.fetchall()]
        except PageDimensionSourceError:
            raise
        except Exception as error:
            raise PageDimensionSourceError("legacy page dimension query failed") from error
        finally:
            if connection is not None:
                connection.close()


def create_page_dimension_adapter(
    source: InquirySourceConfig,
) -> LegacyPageDimensionAdapter:
    """Reuse only the registered site's approved read-only database credential."""
    if source.kind != "legacy_contacts_mysql":
        raise PageDimensionSourceError("configured page dimension source is unsupported")
    dsn = os.environ.get(source.credential_env)
    if not dsn and source.credential_target:
        try:
            dsn = load_windows_generic_credential(source.credential_target)
        except WindowsCredentialError as error:
            raise PageDimensionSourceError(
                "configured page dimension credential is unavailable"
            ) from error
    if not dsn:
        raise PageDimensionSourceError("configured page dimension credential is unavailable")
    return LegacyPageDimensionAdapter(dsn)


def _dimension_row(record: Mapping[str, object]) -> dict[str, object]:
    route_url = record.get("route_url")
    if not isinstance(route_url, str):
        raise PageDimensionSourceError("legacy page dimension route URL is invalid")
    return {
        "route_url": route_url,
        "route_page_id": _identifier(record.get("route_page_id"), allow_none=True),
        "route_source": _route_source(record.get("route_source")),
        "content_page_id": _identifier(record.get("content_page_id"), allow_none=True),
        "template": _template(record.get("template")),
    }


def _identifier(value: object, *, allow_none: bool) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PageDimensionSourceError("legacy page dimension page ID is invalid")
    return value


def _template(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PageDimensionSourceError("legacy page dimension template is invalid")
    return value


def _route_source(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PageDimensionSourceError("legacy page dimension route source is invalid")
    return value.strip()


def _connection_options(dsn: str) -> dict[str, object]:
    if not isinstance(dsn, str) or not dsn.strip():
        raise PageDimensionSourceError("configured page dimension credential is invalid")
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"mysql", "mysql+pymysql"}:
        raise PageDimensionSourceError("page dimension credential must use a MySQL DSN")
    if parsed.query or parsed.fragment or not parsed.hostname or not parsed.path:
        raise PageDimensionSourceError("page dimension credential has an unsupported format")
    if parsed.username is None or parsed.password is None:
        raise PageDimensionSourceError(
            "page dimension credential must contain a database user and password"
        )
    try:
        port = parsed.port or 3306
    except ValueError as error:
        raise PageDimensionSourceError("page dimension credential has an invalid port") from error
    database = unquote(parsed.path.lstrip("/"))
    if not database:
        raise PageDimensionSourceError("page dimension credential must select a database")
    return {
        "host": parsed.hostname,
        "port": port,
        "user": unquote(parsed.username),
        "password": unquote(parsed.password),
        "database": database,
        "connect_timeout": 10,
        "read_timeout": 60,
        "write_timeout": 30,
        "autocommit": True,
    }


def _pymysql_connect(**options: object) -> _Connection:
    try:
        import pymysql
    except ImportError as error:  # pragma: no cover - dependency contract
        raise PageDimensionSourceError(
            "PyMySQL is required for the configured page dimension"
        ) from error
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **options)
