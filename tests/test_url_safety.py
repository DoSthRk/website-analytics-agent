from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

import pytest

from website_analytics.url_safety import sanitize_url_query


@pytest.mark.parametrize(
    "parameter_name",
    (
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "x-goog-api-key",
        "key",
        "password",
        "secret",
        "code",
        "signature",
    ),
)
def test_sensitive_url_parameter_names_are_redacted_but_attribution_is_retained(
    parameter_name: str,
) -> None:
    value = sanitize_url_query(
        f"/landing?{parameter_name}=nonpersisted-value&utm_source=newsletter"
    )
    parameters = dict(parse_qsl(urlsplit(value).query))

    assert parameters[parameter_name] == "[REDACTED]"
    assert parameters["utm_source"] == "newsletter"
