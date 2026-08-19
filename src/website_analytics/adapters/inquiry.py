"""Fixed, read-only adapter for the legacy GeneMedi contact-form database."""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date as CalendarDate
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit, urlunsplit

from website_analytics.models import DateRange, InquirySourceConfig
from website_analytics.url_safety import sanitize_url_query
from website_analytics.windows_credentials import (
    WindowsCredentialError,
    load_windows_generic_credential,
)


_PAGE_ROW_CAP = 50_000
_PAGE_QUERY_LIMIT = _PAGE_ROW_CAP + 1
_METRICS = (
    "storedSubmissions",
    "quarantinedSubmissions",
    "nonQuarantinedSubmissions",
)
_DATE_BOUNDARY = (
    "contacts.submission_date is written by the legacy website server calendar; "
    "it can differ from the configured site timezone, GA4, and GSC boundaries."
)
_DAILY_QUERY = """
SELECT submission_date AS date,
       COUNT(*) AS stored_submissions,
       COALESCE(SUM(CASE WHEN email_sent_to = 'SPAM_QUARANTINE' THEN 1 ELSE 0 END), 0)
         AS quarantined_submissions,
       COALESCE(SUM(CASE WHEN email_sent_to <> 'SPAM_QUARANTINE' OR email_sent_to IS NULL
                         THEN 1 ELSE 0 END), 0)
         AS non_quarantined_submissions
FROM contacts
WHERE submission_date >= %s AND submission_date <= %s
GROUP BY submission_date
ORDER BY submission_date
"""
_PAGES_QUERY = f"""
SELECT PageURL AS source_url,
       COUNT(*) AS stored_submissions,
       COALESCE(SUM(CASE WHEN email_sent_to = 'SPAM_QUARANTINE' THEN 1 ELSE 0 END), 0)
         AS quarantined_submissions,
       COALESCE(SUM(CASE WHEN email_sent_to <> 'SPAM_QUARANTINE' OR email_sent_to IS NULL
                         THEN 1 ELSE 0 END), 0)
         AS non_quarantined_submissions
FROM contacts
WHERE submission_date >= %s AND submission_date <= %s
  AND PageURL IS NOT NULL AND PageURL <> ''
GROUP BY PageURL
ORDER BY stored_submissions DESC, source_url
LIMIT {_PAGE_QUERY_LIMIT}
"""


class InquirySourceError(RuntimeError):
    """Raised when a configured inquiry source cannot safely be queried."""


class _Cursor(Protocol):
    def execute(self, query: str, args: Sequence[object] | None = None) -> object:
        """Execute a prepared, fixed query."""

    def fetchall(self) -> Sequence[Mapping[str, object]]:
        """Return mapping rows."""


class _Connection(Protocol):
    def cursor(self) -> Any:
        """Create a mapping cursor."""

    def close(self) -> object:
        """Close the read-only connection."""


Connector = Callable[..., _Connection]
InquiryRow = dict[str, str | float]


@dataclass(frozen=True)
class InquiryQueryResult:
    """Normalized inquiry details, totals, and explicit page-detail completeness."""

    daily: tuple[InquiryRow, ...]
    pages: tuple[InquiryRow, ...]
    totals: dict[str, float]
    page_rows_truncated: bool


class LegacyContactsAdapter:
    """Query only aggregate, non-PII facts from the approved ``contacts`` table."""

    def __init__(self, dsn: str, *, connector: Connector | None = None) -> None:
        self._connection_options = _connection_options(dsn)
        self._connector = connector or _pymysql_connect

    def query(self, date_range: DateRange) -> InquiryQueryResult:
        connection: _Connection | None = None
        try:
            connection = self._connector(**self._connection_options)
            with connection.cursor() as cursor:
                cursor.execute(
                    _DAILY_QUERY,
                    (date_range.start.isoformat(), date_range.end.isoformat()),
                )
                daily = tuple(_daily_row(row) for row in cursor.fetchall())
                cursor.execute(
                    _PAGES_QUERY,
                    (date_range.start.isoformat(), date_range.end.isoformat()),
                )
                raw_page_rows = cursor.fetchall()
                pages = tuple(_page_row(row) for row in raw_page_rows[:_PAGE_ROW_CAP])
        except InquirySourceError:
            raise
        except Exception as error:
            raise InquirySourceError("legacy inquiry source query failed") from error
        finally:
            if connection is not None:
                connection.close()

        return InquiryQueryResult(
            daily=daily,
            pages=pages,
            totals={metric: sum(_metric(row, metric) for row in daily) for metric in _METRICS},
            page_rows_truncated=len(raw_page_rows) > _PAGE_ROW_CAP,
        )


def create_inquiry_adapter(source: InquirySourceConfig) -> LegacyContactsAdapter:
    """Load a fixed database connection from a named local environment variable."""
    if source.kind != "legacy_contacts_mysql":
        raise InquirySourceError("configured inquiry source is unsupported")
    dsn = os.environ.get(source.credential_env)
    if not dsn and source.credential_target:
        try:
            dsn = load_windows_generic_credential(source.credential_target)
        except WindowsCredentialError as error:
            raise InquirySourceError("configured inquiry credential is unavailable") from error
    if not dsn:
        raise InquirySourceError("configured inquiry credential is unavailable")
    return LegacyContactsAdapter(dsn)


def inquiry_source_data(
    adapter: LegacyContactsAdapter, date_range: DateRange
) -> tuple[Mapping[str, list[InquiryRow]], dict[str, float], Mapping[str, Any]]:
    """Return approved details and metric totals without exposing rows from ``contacts``."""
    result = adapter.query(date_range)
    return (
        {"Inquiry Daily": list(result.daily), "Inquiry Pages": list(result.pages)},
        result.totals,
        {
            "status": "partial" if result.page_rows_truncated else "ok",
            "date_boundary": _DATE_BOUNDARY,
            "details": {
                "Inquiry Pages": {
                    "rows": len(result.pages),
                    "row_cap": _PAGE_ROW_CAP,
                    "truncated": result.page_rows_truncated,
                }
            },
        },
    )


def _connection_options(dsn: str) -> dict[str, object]:
    """Parse a MySQL-only DSN without returning or logging the secret value."""
    if not isinstance(dsn, str) or not dsn.strip():
        raise InquirySourceError("configured inquiry credential is invalid")
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"mysql", "mysql+pymysql"}:
        raise InquirySourceError("inquiry credential must use a MySQL DSN")
    if parsed.query or parsed.fragment or not parsed.hostname or not parsed.path:
        raise InquirySourceError("inquiry credential has an unsupported format")
    if parsed.username is None or parsed.password is None:
        raise InquirySourceError("inquiry credential must contain a database user and password")
    try:
        port = parsed.port or 3306
    except ValueError as error:
        raise InquirySourceError("inquiry credential has an invalid database port") from error
    database = unquote(parsed.path.lstrip("/"))
    if not database:
        raise InquirySourceError("inquiry credential must select a database")
    return {
        "host": parsed.hostname,
        "port": port,
        "user": unquote(parsed.username),
        "password": unquote(parsed.password),
        "database": database,
        "connect_timeout": 10,
        "read_timeout": 30,
        "write_timeout": 30,
        "autocommit": True,
    }


def _pymysql_connect(**options: object) -> _Connection:
    try:
        import pymysql
    except ImportError as error:  # pragma: no cover - dependency contract
        raise InquirySourceError("PyMySQL is required for the configured inquiry source") from error
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **options)


def _daily_row(record: Mapping[str, object]) -> InquiryRow:
    date_value = record.get("date")
    if isinstance(date_value, CalendarDate):
        date = date_value.isoformat()
    elif isinstance(date_value, str):
        try:
            date = CalendarDate.fromisoformat(date_value).isoformat()
        except ValueError as error:
            raise InquirySourceError("legacy inquiry daily row has an invalid date") from error
    else:
        raise InquirySourceError("legacy inquiry daily row has an invalid date")
    return {"date": date, **_metrics(record)}


def _page_row(record: Mapping[str, object]) -> InquiryRow:
    source_url = record.get("source_url")
    if not isinstance(source_url, str) or not source_url.strip():
        raise InquirySourceError("legacy inquiry page row has an invalid source URL")
    return {"sourceUrl": _safe_source_url(source_url), **_metrics(record)}


def _safe_source_url(value: str) -> str:
    """Keep only an HTTP(S) page identity; legacy URLs can contain form data."""
    sanitized = sanitize_url_query(value.strip())
    parts = urlsplit(sanitized)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return "[UNATTRIBUTED]"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _metrics(record: Mapping[str, object]) -> dict[str, float]:
    return {
        "storedSubmissions": _number(record.get("stored_submissions")),
        "quarantinedSubmissions": _number(record.get("quarantined_submissions")),
        "nonQuarantinedSubmissions": _number(record.get("non_quarantined_submissions")),
    }


def _metric(record: Mapping[str, object], name: str) -> float:
    value = record.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InquirySourceError("legacy inquiry metric is not numeric")
    return float(value)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise InquirySourceError("legacy inquiry metric is not numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise InquirySourceError("legacy inquiry metric is invalid")
    return number
