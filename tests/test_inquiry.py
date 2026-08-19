from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

import website_analytics.adapters.inquiry as inquiry_adapter
from website_analytics.adapters.inquiry import (
    InquirySourceError,
    LegacyContactsAdapter,
    create_inquiry_adapter,
    inquiry_source_data,
)
from website_analytics.models import DateRange, InquirySourceConfig


class _Cursor:
    def __init__(self, responses):
        self._responses = list(responses)
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, args=None):
        self.executed.append((query, args))

    def fetchall(self):
        return self._responses.pop(0)


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def test_legacy_contacts_adapter_uses_fixed_aggregate_queries_and_never_reads_pii() -> None:
    cursor = _Cursor(
        [
            [
                {
                    "date": date(2026, 8, 3),
                    "stored_submissions": Decimal("4"),
                    "quarantined_submissions": Decimal("1"),
                    "non_quarantined_submissions": Decimal("3"),
                }
            ],
            [
                {
                    "source_url": "https://www.genemedi.net/i/gmp-column?token=secret",
                    "stored_submissions": 2,
                    "quarantined_submissions": 1,
                    "non_quarantined_submissions": 1,
                }
            ],
        ]
    )
    connection = _Connection(cursor)
    captured_options = {}

    def connector(**options):
        captured_options.update(options)
        return connection

    adapter = LegacyContactsAdapter(
        "mysql+pymysql://analytics:password@db.example.test:3307/genemedi_net",
        connector=connector,
    )

    result = adapter.query(DateRange(start=date(2026, 8, 3), end=date(2026, 8, 9)))

    assert captured_options["host"] == "db.example.test"
    assert captured_options["port"] == 3307
    assert captured_options["database"] == "genemedi_net"
    assert connection.closed is True
    assert result.totals == {
        "storedSubmissions": 4.0,
        "quarantinedSubmissions": 1.0,
        "nonQuarantinedSubmissions": 3.0,
    }
    assert result.pages[0]["sourceUrl"] == "https://www.genemedi.net/i/gmp-column"
    assert result.page_rows_truncated is False
    queried_sql = "\n".join(query for query, _ in cursor.executed)
    assert "SELECT *" not in queried_sql
    assert "Email" not in queried_sql
    assert "Inquiry" not in queried_sql
    assert all(args == ("2026-08-03", "2026-08-09") for _, args in cursor.executed)


def test_inquiry_source_data_marks_page_detail_as_partial_at_the_cap() -> None:
    page_rows = [
        {
            "source_url": f"https://www.genemedi.net/i/gmp-{index}",
            "stored_submissions": 1,
            "quarantined_submissions": 0,
            "non_quarantined_submissions": 1,
        }
        for index in range(50_001)
    ]
    cursor = _Cursor(
        [
            [
                {
                    "date": "2026-08-03",
                    "stored_submissions": 50_000,
                    "quarantined_submissions": 0,
                    "non_quarantined_submissions": 50_000,
                }
            ],
            page_rows,
        ]
    )
    adapter = LegacyContactsAdapter(
        "mysql://analytics:password@db.example.test/genemedi_net",
        connector=lambda **_: _Connection(cursor),
    )

    details, totals, metadata = inquiry_source_data(
        adapter, DateRange(start=date(2026, 8, 3), end=date(2026, 8, 9))
    )

    assert len(details["Inquiry Pages"]) == 50_000
    assert totals["nonQuarantinedSubmissions"] == 50_000.0
    assert metadata["status"] == "partial"


@pytest.mark.parametrize(
    "dsn",
    [
        "postgres://user:password@db.example.test/analytics",
        "mysql://user@db.example.test/analytics",
        "mysql://user:password@db.example.test/analytics?arbitrary=1",
    ],
)
def test_legacy_contacts_adapter_rejects_unapproved_dsn_shapes(dsn: str) -> None:
    with pytest.raises(InquirySourceError):
        LegacyContactsAdapter(dsn)


def test_create_inquiry_adapter_reads_only_named_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = InquirySourceConfig(
        kind="legacy_contacts_mysql",
        credential_env="WEBSITE_ANALYTICS_DEMO_INQUIRY_DSN",
    )
    monkeypatch.setenv(
        source.credential_env, "mysql://analytics:password@db.example.test/analytics"
    )

    assert isinstance(create_inquiry_adapter(source), LegacyContactsAdapter)


def test_create_inquiry_adapter_can_read_only_the_configured_windows_credential_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = InquirySourceConfig(
        kind="legacy_contacts_mysql",
        credential_env="WEBSITE_ANALYTICS_DEMO_INQUIRY_DSN",
        credential_target="WebsiteAnalytics/demo/inquiry-dsn",
    )
    observed = []
    monkeypatch.setattr(
        inquiry_adapter,
        "load_windows_generic_credential",
        lambda target: observed.append(target) or "mysql://analytics:password@db.example.test/analytics",
    )

    assert isinstance(create_inquiry_adapter(source), LegacyContactsAdapter)
    assert observed == ["WebsiteAnalytics/demo/inquiry-dsn"]
