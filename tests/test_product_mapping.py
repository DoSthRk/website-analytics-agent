from __future__ import annotations

from pathlib import Path

from website_analytics.product_mapping import build_product_report, load_product_mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAPPING_PATH = PROJECT_ROOT / "config" / "product_mappings" / "genemedi-net.yaml"


def test_approved_mapping_aggregates_only_reportable_product_pages() -> None:
    mapping = load_product_mapping(MAPPING_PATH, "genemedi-net")
    assert mapping is not None

    current = {
        "GA4 Pages": [
            {"landingPagePlusQueryString": "/i/GMP-ISOEx-Mag-MX1", "sessions": 4.0},
            {"landingPagePlusQueryString": "/i/gmp-generic-column", "sessions": 7.0},
            {"landingPagePlusQueryString": "/i/PurProX-AAVFull-Enrichment-Kit?ref=weekly", "sessions": 1.0},
            {"landingPagePlusQueryString": "/i/truex-aav-titration-elisa-kit", "sessions": 2.0},
            {"landingPagePlusQueryString": "/pdf/purprox-aaveasy-flyer.pdf", "sessions": 9.0},
            {"landingPagePlusQueryString": "/i/anti-payload-antibody-for-adcs", "sessions": 3.0},
            {"landingPagePlusQueryString": "/i/protocols-application-antibody-titration", "sessions": 6.0},
        ],
        "GSC Pages": [
            {"page": "https://www.genemedi.net/i/gmp-isoex-mag-mx1", "clicks": 1.0, "impressions": 10.0},
            {"page": "https://www.genemedi.net/i/gmp-generic-column", "clicks": 5.0, "impressions": 50.0},
            {"page": "https://www.genemedi.net/i/purprox-aavfull-enrichment-kit", "clicks": 2.0, "impressions": 20.0},
            {"page": "https://www.genemedi.net/i/truex-aav-titration-elisa-kit", "clicks": 3.0, "impressions": 30.0},
            {"page": "https://www.genemedi.net/pdf/purprox-aaveasy-flyer.pdf", "clicks": 8.0, "impressions": 80.0},
        ],
    }
    previous = {
        "GA4 Pages": [
            {"landingPagePlusQueryString": "/i/gmp-generic-column", "sessions": 2.0},
            {"landingPagePlusQueryString": "/i/purprox-aavfull-enrichment-kit", "sessions": 1.0},
        ],
        "GSC Pages": [
            {"page": "https://www.genemedi.net/i/gmp-generic-column", "clicks": 1.0, "impressions": 10.0},
            {"page": "https://www.genemedi.net/i/purprox-aavfull-enrichment-kit", "clicks": 1.0, "impressions": 10.0},
        ],
    }

    report = build_product_report(mapping, current, previous)
    summary = {row["reportLineId"]: row for row in report["reportLines"]}

    assert summary["GMP"] == {
        "reportLineId": "GMP",
        "reportLine": "GMP 系列",
        "currentCanonicalPages": 1,
        "ga4SessionsCurrent": 7.0,
        "ga4SessionsPrevious": 2.0,
        "ga4SessionsDelta": 5.0,
        "gscClicksCurrent": 5.0,
        "gscClicksPrevious": 1.0,
        "gscClicksDelta": 4.0,
        "gscImpressionsCurrent": 50.0,
        "gscImpressionsPrevious": 10.0,
        "gscImpressionsDelta": 40.0,
        "gscCtrCurrent": 0.1,
        "gscCtrPrevious": 0.1,
        "gscCtrDelta": 0.0,
    }
    assert summary["SOLIDEX"]["currentCanonicalPages"] == 1
    assert summary["SOLIDEX"]["ga4SessionsCurrent"] == 4.0
    assert summary["AAV_PROCESSING"]["currentCanonicalPages"] == 2
    assert summary["AAV_PROCESSING"]["ga4SessionsCurrent"] == 3.0
    assert summary["AAV_PROCESSING"]["gscClicksCurrent"] == 5.0

    pages = {row["canonicalPath"]: row for row in report["pageMappings"]}
    assert pages["/i/gmp-isoex-mag-mx1"]["reportLineId"] == "SOLIDEX"
    assert pages["/pdf/purprox-aaveasy-flyer.pdf"]["includeInProductReport"] is False
    assert pages["/i/anti-payload-antibody-for-adcs"]["includeInProductReport"] is False
    assert pages["/i/protocols-application-antibody-titration"]["productLineId"] == "UNCLASSIFIED"


def test_absent_mapping_is_not_an_error_for_other_registered_sites(tmp_path: Path) -> None:
    assert load_product_mapping(tmp_path / "missing.yaml", "demo") is None
