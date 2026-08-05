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
