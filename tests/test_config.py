from __future__ import annotations

import pytest

from website_analytics.config import ConfigError, load_sites, require_site
from website_analytics.models import SiteConfig


def test_load_sites_converts_demo_config_and_selects_registered_site(tmp_path) -> None:
    config_path = tmp_path / "sites.yaml"
    config_path.write_text(
        """
sites:
  demo:
    display_name: Demo site
    domains:
      - example.com
    timezone: Asia/Shanghai
    ga4_property_id: 123
    gsc_property_url: sc-domain:example.com
    key_events:
      - generate_lead
""".lstrip(),
        encoding="utf-8",
    )

    sites = load_sites(config_path)
    site = require_site(sites, "demo")

    assert isinstance(site, SiteConfig)
    assert site.site_key == "demo"
    assert site.ga4_property_id == "123"
    assert site.domains == ("example.com",)
    assert site.key_events == ("generate_lead",)


def test_load_sites_accepts_fixed_inquiry_source_without_a_credential_value(tmp_path) -> None:
    config_path = tmp_path / "sites.yaml"
    config_path.write_text(
        """
sites:
  demo:
    display_name: Demo site
    domains: [example.com]
    timezone: Asia/Shanghai
    ga4_property_id: 123
    gsc_property_url: sc-domain:example.com
    inquiry_source:
      kind: legacy_contacts_mysql
      credential_env: WEBSITE_ANALYTICS_DEMO_INQUIRY_DSN
""".lstrip(),
        encoding="utf-8",
    )

    site = require_site(load_sites(config_path), "demo")

    assert site.inquiry_source is not None
    assert site.inquiry_source.kind == "legacy_contacts_mysql"
    assert site.inquiry_source.credential_env == "WEBSITE_ANALYTICS_DEMO_INQUIRY_DSN"
    assert site.inquiry_source.credential_target is None


def test_load_sites_accepts_a_windows_credential_target_for_the_fixed_inquiry_source(tmp_path) -> None:
    config_path = tmp_path / "sites.yaml"
    config_path.write_text(
        """
sites:
  demo:
    display_name: Demo
    domains: [example.com]
    timezone: Asia/Shanghai
    ga4_property_id: 123
    gsc_property_url: sc-domain:example.com
    inquiry_source:
      kind: legacy_contacts_mysql
      credential_env: WEBSITE_ANALYTICS_DEMO_INQUIRY_DSN
      credential_target: WebsiteAnalytics/demo/inquiry-dsn
""".lstrip(),
        encoding="utf-8",
    )

    site = require_site(load_sites(config_path), "demo")

    assert site.inquiry_source is not None
    assert site.inquiry_source.credential_target == "WebsiteAnalytics/demo/inquiry-dsn"


def test_load_sites_rejects_unsafe_inquiry_credential_environment_name(tmp_path) -> None:
    config_path = tmp_path / "sites.yaml"
    config_path.write_text(
        """
sites:
  demo:
    display_name: Demo site
    domains: [example.com]
    timezone: Asia/Shanghai
    ga4_property_id: 123
    gsc_property_url: sc-domain:example.com
    inquiry_source:
      kind: legacy_contacts_mysql
      credential_env: PATH
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="credential_env"):
        load_sites(config_path)


def test_load_sites_rejects_an_unsafe_inquiry_credential_target(tmp_path) -> None:
    config_path = tmp_path / "sites.yaml"
    config_path.write_text(
        """
sites:
  demo:
    display_name: Demo
    domains: [example.com]
    timezone: Asia/Shanghai
    ga4_property_id: 123
    gsc_property_url: sc-domain:example.com
    inquiry_source:
      kind: legacy_contacts_mysql
      credential_env: WEBSITE_ANALYTICS_DEMO_INQUIRY_DSN
      credential_target: ../other
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="credential_target"):
        load_sites(config_path)


def test_require_site_rejects_unknown_site() -> None:
    with pytest.raises(ConfigError, match="site 'unknown' is not registered"):
        require_site({}, "unknown")


def test_load_sites_rejects_blank_required_value(tmp_path) -> None:
    config_path = tmp_path / "sites.yaml"
    config_path.write_text(
        """
sites:
  demo:
    display_name: ""
    domains:
      - example.com
    timezone: Asia/Shanghai
    ga4_property_id: 123
    gsc_property_url: sc-domain:example.com
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="display_name"):
        load_sites(config_path)


def test_load_sites_rejects_duplicate_yaml_site_key(tmp_path) -> None:
    config_path = tmp_path / "sites.yaml"
    config_path.write_text(
        """
sites:
  demo:
    display_name: First demo
    domains:
      - example.com
    timezone: Asia/Shanghai
    ga4_property_id: 123
    gsc_property_url: sc-domain:example.com
  demo:
    display_name: Second demo
    domains:
      - example.com
    timezone: Asia/Shanghai
    ga4_property_id: 456
    gsc_property_url: sc-domain:example.com
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="duplicate YAML key 'demo'"):
        load_sites(config_path)


def test_load_sites_rejects_site_keys_that_collide_after_trimming(tmp_path) -> None:
    config_path = tmp_path / "sites.yaml"
    config_path.write_text(
        """
sites:
  demo:
    display_name: First demo
    domains:
      - example.com
    timezone: Asia/Shanghai
    ga4_property_id: 123
    gsc_property_url: sc-domain:example.com
  ' demo ':
    display_name: Second demo
    domains:
      - example.com
    timezone: Asia/Shanghai
    ga4_property_id: 456
    gsc_property_url: sc-domain:example.com
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="duplicate site key 'demo'"):
        load_sites(config_path)


def test_load_sites_rejects_unexpected_root_field(tmp_path) -> None:
    config_path = tmp_path / "sites.yaml"
    config_path.write_text(
        """
sites:
  demo:
    display_name: Demo site
    domains:
      - example.com
    timezone: Asia/Shanghai
    ga4_property_id: 123
    gsc_property_url: sc-domain:example.com
environment: production
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unexpected root field 'environment'"):
        load_sites(config_path)


def test_load_sites_rejects_unexpected_site_field(tmp_path) -> None:
    config_path = tmp_path / "sites.yaml"
    config_path.write_text(
        """
sites:
  demo:
    display_name: Demo site
    domains:
      - example.com
    timezone: Asia/Shanghai
    ga4_property_id: 123
    gsc_property_url: sc-domain:example.com
    key_event:
      - generate_lead
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError, match="site 'demo' has unexpected field 'key_event'"
    ):
        load_sites(config_path)
