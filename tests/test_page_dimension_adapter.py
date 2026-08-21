import pytest

import website_analytics.adapters.page_dimension as page_adapter
from website_analytics.adapters.page_dimension import (
    LegacyPageDimensionAdapter,
    PageDimensionSourceError,
    create_page_dimension_adapter,
)
from website_analytics.models import InquirySourceConfig


class _Cursor:
    def __init__(self, response):
        self.response = response
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, args=None):
        self.executed.append((query, args))

    def fetchall(self):
        return self.response


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def test_adapter_reads_only_route_ids_and_templates() -> None:
    cursor = _Cursor(
        [
            {
                "route_url": "i/product",
                "route_page_id": 10,
                "content_page_id": 10,
                "template": "indexwithSideBar",
            }
        ]
    )
    connection = _Connection(cursor)
    adapter = LegacyPageDimensionAdapter(
        "mysql://analytics:password@db.example.test/genemedi_net",
        connector=lambda **_: connection,
    )

    assert adapter.query()[0]["route_url"] == "i/product"
    assert connection.closed is True
    query = cursor.executed[0][0]
    assert "urltable" in query and "pages" in query
    assert "p.content" not in query.casefold()
    assert "select *" not in query.casefold()
    assert "title" not in query.casefold()
    assert "p.name" not in query.casefold()
    assert cursor.executed[0][1] is None


def test_create_adapter_uses_only_registered_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    source = InquirySourceConfig(
        kind="legacy_contacts_mysql",
        credential_env="WEBSITE_ANALYTICS_DEMO_INQUIRY_DSN",
    )
    monkeypatch.setenv(
        source.credential_env,
        "mysql://analytics:password@db.example.test/genemedi_net",
    )
    assert isinstance(create_page_dimension_adapter(source), LegacyPageDimensionAdapter)


def test_adapter_rejects_non_mysql_credentials() -> None:
    with pytest.raises(PageDimensionSourceError):
        LegacyPageDimensionAdapter("postgres://user:password@db.example.test/analytics")
