from pathlib import Path

import pytest

from website_analytics.page_classification import (
    PageClassificationError,
    build_page_dimension,
    canonical_page_path,
    load_page_classification,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT_ROOT / "config" / "page_classifications" / "genemedi-net.yaml"


def test_template_dimension_distinguishes_product_and_information_under_i() -> None:
    config = load_page_classification(CONFIG, "genemedi-net")
    dimension = build_page_dimension(
        config,
        [
            _row("i/gmp-product", 1, 1, "indexwithSideBar"),
            _row("i/gmp-information", 2, 2, "index-general"),
            _row("i/GMP-AD-Pig-9", 1066, 1066, ""),
            _row("about", 3111, 3111, "indexwithSidebar_about_us"),
            _row("i/orphan", 9000, None, None),
            _row("", 9991, 9991, "index"),
            _row("", 60892, None, None),
        ],
    )

    assert dimension.classify("/i/gmp-product").page_class == "product_page"
    assert dimension.classify("https://www.genemedi.net/i/gmp-information?x=1").page_class == (
        "information_page"
    )
    assert dimension.classify("/i/GMP-AD-Pig-9").classification_status == "manual_override"
    assert dimension.classify("/about").page_class == "information_page"
    assert dimension.classify("/i/orphan").page_class == "invalid_broken"
    assert dimension.classify("/").page_class == "information_page"
    assert dimension.classify("/").has_orphan_route is True
    assert dimension.classify("/i/not-mapped").page_class == "unknown_unmapped"
    assert dimension.classify("/pdf/file.pdf").page_class == "pdf_asset"
    assert dimension.summary == {
        "sourceRows": 7,
        "canonicalPaths": 6,
        "productPages": 2,
        "informationPages": 3,
        "invalidBrokenPaths": 1,
        "orphanRoutes": 2,
        "duplicatePaths": 1,
        "overridesApplied": 2,
    }


def test_canonical_path_decodes_legacy_space_and_removes_query_fragment() -> None:
    assert canonical_page_path("https://www.genemedi.net/i/A%20B/?x=1#y") == "/i/a b"
    assert canonical_page_path("i/A B") == "/i/a b"


def test_page_classification_rejects_duplicate_override_ids(tmp_path: Path) -> None:
    path = tmp_path / "classification.yaml"
    path.write_text(
        """
version: "1"
site: genemedi-net
overrides:
  - {page_id: 1, page_class: product_page, reason: first}
  - {page_id: 1, page_class: information_page, reason: duplicate}
""".lstrip(),
        encoding="utf-8",
    )
    with pytest.raises(PageClassificationError, match="unique"):
        load_page_classification(path, "genemedi-net")


def _row(url: str, route_id: int, content_id: int | None, template: str | None) -> dict:
    return {
        "route_url": url,
        "route_page_id": route_id,
        "content_page_id": content_id,
        "template": template,
    }
