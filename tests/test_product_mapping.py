from __future__ import annotations

from pathlib import Path

from website_analytics.page_classification import (
    build_page_dimension,
    load_page_classification,
)
from website_analytics.product_mapping import (
    build_product_report,
    load_product_mapping,
    match_product_rule,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAPPING_PATH = PROJECT_ROOT / "config" / "product_mappings" / "genemedi-net.yaml"
CLASSIFICATION_PATH = (
    PROJECT_ROOT / "config" / "page_classifications" / "genemedi-net.yaml"
)


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
            {"landingPagePlusQueryString": "/i/gmp-information-guide", "sessions": 100.0},
        ],
        "GSC Pages": [
            {"page": "https://www.genemedi.net/i/gmp-isoex-mag-mx1", "clicks": 1.0, "impressions": 10.0},
            {"page": "https://www.genemedi.net/i/gmp-generic-column", "clicks": 5.0, "impressions": 50.0},
            {"page": "https://www.genemedi.net/i/purprox-aavfull-enrichment-kit", "clicks": 2.0, "impressions": 20.0},
            {"page": "https://www.genemedi.net/i/truex-aav-titration-elisa-kit", "clicks": 3.0, "impressions": 30.0},
            {"page": "https://www.genemedi.net/pdf/purprox-aaveasy-flyer.pdf", "clicks": 8.0, "impressions": 80.0},
            {"page": "https://www.genemedi.net/i/gmp-information-guide", "clicks": 20.0, "impressions": 200.0},
        ],
        "Inquiry Pages": [
            {
                "sourceUrl": "https://www.genemedi.net/i/gmp-generic-column",
                "storedSubmissions": 3.0,
                "quarantinedSubmissions": 1.0,
                "nonQuarantinedSubmissions": 2.0,
            },
            {
                "sourceUrl": "https://www.genemedi.net/i/purprox-aavfull-enrichment-kit",
                "storedSubmissions": 2.0,
                "quarantinedSubmissions": 0.0,
                "nonQuarantinedSubmissions": 2.0,
            },
            {
                "sourceUrl": "https://www.genemedi.net/i/anti-payload-antibody-for-adcs",
                "storedSubmissions": 5.0,
                "quarantinedSubmissions": 0.0,
                "nonQuarantinedSubmissions": 5.0,
            },
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
        "Inquiry Pages": [
            {
                "sourceUrl": "https://www.genemedi.net/i/gmp-generic-column",
                "storedSubmissions": 1.0,
                "quarantinedSubmissions": 0.0,
                "nonQuarantinedSubmissions": 1.0,
            }
        ],
    }

    classification = load_page_classification(CLASSIFICATION_PATH, "genemedi-net")
    dimension = build_page_dimension(
        classification,
        [
            _dimension_row("i/gmp-isoex-mag-mx1", 10, "indexwithSideBar"),
            _dimension_row("i/gmp-generic-column", 11, "indexwithSideBa_product"),
            _dimension_row("i/purprox-aavfull-enrichment-kit", 12, "GCT_Purpro_SideBar"),
            _dimension_row("i/truex-aav-titration-elisa-kit", 13, "indexwithSideBar-aav"),
            _dimension_row("i/anti-payload-antibody-for-adcs", 14, "indexwithSideBar_payload"),
            _dimension_row("i/protocols-application-antibody-titration", 15, "index-general"),
            _dimension_row("i/gmp-information-guide", 16, "index-general"),
        ],
    )
    report = build_product_report(mapping, current, previous, dimension)
    summary = {row["reportLineId"]: row for row in report["reportLines"]}

    diagnostics = summary["DIAGNOSTICS_OTHER"]
    assert diagnostics["categoryL1"] == "DIAGNOSTICS"
    assert diagnostics["currentCanonicalPages"] == 1
    assert diagnostics["ga4SessionsCurrent"] == 7.0
    assert diagnostics["ga4SessionsPrevious"] == 2.0
    assert diagnostics["gscClicksCurrent"] == 5.0
    assert diagnostics["gscCtrCurrent"] == 0.1
    assert summary["SOLIDEX"]["currentCanonicalPages"] == 1
    assert summary["SOLIDEX"]["ga4SessionsCurrent"] == 4.0
    assert summary["AAV_PURIFICATION"]["ga4SessionsCurrent"] == 1.0
    assert summary["AAV_TITRATION"]["ga4SessionsCurrent"] == 2.0
    assert summary["PAYLOAD"]["ga4SessionsCurrent"] == 3.0

    inquiry_summary = {row["reportLineId"]: row for row in report["inquiryReportLines"]}
    assert inquiry_summary["DIAGNOSTICS_OTHER"]["nonQuarantinedSubmissionsCurrent"] == 2.0
    assert inquiry_summary["AAV_PURIFICATION"]["nonQuarantinedSubmissionsCurrent"] == 2.0
    assert inquiry_summary["PAYLOAD"]["nonQuarantinedSubmissionsCurrent"] == 5.0
    assert inquiry_summary["SOLIDEX"]["storedSubmissionsCurrent"] == 0.0

    pages = {row["canonicalPath"]: row for row in report["pageMappings"]}
    assert pages["/i/gmp-isoex-mag-mx1"]["reportLineId"] == "SOLIDEX"
    assert pages["/pdf/purprox-aaveasy-flyer.pdf"]["includeInProductReport"] is False
    assert pages["/i/anti-payload-antibody-for-adcs"]["reportLineId"] == "PAYLOAD"
    assert pages["/i/anti-payload-antibody-for-adcs"]["includeInProductReport"] is True
    assert pages["/i/protocols-application-antibody-titration"]["productLineId"] == ""
    assert pages["/i/gmp-information-guide"]["pageClass"] == "information_page"
    assert pages["/i/gmp-information-guide"]["includeInProductReport"] is False
    page_types = {row["pageTypeId"]: row for row in report["pageTypeLines"]}
    assert page_types["information_page"]["ga4SessionsCurrent"] == 106.0
    assert page_types["product_page"]["ga4SessionsCurrent"] == 17.0
    assert report["classificationCoverage"]["ga4ClassifiedRate"] == 123.0 / 132.0


def test_absent_mapping_is_not_an_error_for_other_registered_sites(tmp_path: Path) -> None:
    assert load_product_mapping(tmp_path / "missing.yaml", "demo") is None


def test_approved_template_supplements_preserve_payload_and_purprox() -> None:
    mapping = load_product_mapping(MAPPING_PATH, "genemedi-net")
    assert mapping is not None
    payload = match_product_rule(
        mapping,
        "/i/adc-kit-without-product-token",
        "product_page",
        "indexwithSideBar_payload",
    )
    purification = match_product_rule(
        mapping,
        "/i/aav-process-kit",
        "product_page",
        "GCT_Purpro_SideBar",
    )
    solidex = match_product_rule(
        mapping,
        "/i/cell-separation-kit",
        "product_page",
        "SOLIDEX.htm",
    )
    assert payload is not None and payload.report_line_id == "PAYLOAD"
    assert purification is not None and purification.report_line_id == "AAV_PURIFICATION"
    assert solidex is not None and solidex.report_line_id == "SOLIDEX"


def _dimension_row(url: str, page_id: int, template: str) -> dict[str, object]:
    return {
        "route_url": url,
        "route_page_id": page_id,
        "content_page_id": page_id,
        "template": template,
    }
