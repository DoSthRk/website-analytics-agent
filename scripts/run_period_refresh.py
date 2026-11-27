"""Daily refresh of the Feishu period tables, independent of the production sync.

1. Read the latest finalized day from the V3 overview table in Feishu.
2. Recompute the last ``--days-back`` days up to that day from the sync's
   cache, with the current rules, and save them to the daily-facts store.
3. Fetch the GA4 crawler fingerprint report for the same days (read-only) and
   save it to the fingerprint store.
4. Rebuild every period row from the store and write only changed rows; the
   cleaned product landing sessions come from the crawler rule.
5. With --alert-config, send the weekly anomaly digest to the ops group once
   per complete week, after the write was read back.

Without --apply step 4 only reports the plan, and nothing is saved. GA4 needs
GOOGLE_APPLICATION_CREDENTIALS and, on the server, the HTTP(S)_PROXY settings.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from website_analytics.adapters.page_dimension import create_page_dimension_adapter  # noqa: E402
from website_analytics.config import load_sites, require_site  # noqa: E402
from website_analytics.crawler_traffic import REPORT_DIMENSIONS, fingerprint_days, save_fingerprint_days  # noqa: E402
from website_analytics.daily_facts_store import save_days  # noqa: E402
from website_analytics.feishu_records import LarkCLIRecordClient  # noqa: E402
from website_analytics.feishu_v3 import load_json_object  # noqa: E402
from website_analytics.feishu_v3_sync import load_feishu_v3_target  # noqa: E402
from website_analytics.history_recompute import RuleSet, recompute_days  # noqa: E402
from website_analytics.information_mapping import load_information_mapping  # noqa: E402
from website_analytics.page_classification import build_page_dimension, load_page_classification  # noqa: E402
from website_analytics.feishu_tables import lark_runtime, run_lark  # noqa: E402
from website_analytics.period_alerts import load_alert_config, send_weekly_alert  # noqa: E402
from website_analytics.period_facts_job import refresh_period_tables  # noqa: E402
from website_analytics.models import DateRange  # noqa: E402
from website_analytics.product_mapping import load_product_mapping  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="genemedi-net")
    parser.add_argument("--site-config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True, help="KEY=VALUE file holding the read-only DB credential")
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--page-classification", type=Path, required=True)
    parser.add_argument("--information-mapping", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--v3-target", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--period-target", type=Path, required=True)
    parser.add_argument("--exclusions", type=Path, required=True)
    parser.add_argument("--crawler-store", type=Path, required=True, help="GA4 fingerprint days, one JSON per day")
    parser.add_argument("--crawler-rule", type=Path, required=True)
    parser.add_argument("--days-back", type=int, default=14)
    parser.add_argument("--alert-config", type=Path, help="server-only JSON with chat_id and dashboard_url")
    parser.add_argument("--alert-state", type=Path, help="JSON remembering the last week sent")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if (args.alert_config is None) != (args.alert_state is None):
        parser.error("--alert-config and --alert-state go together")
    return args


def _load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def latest_final_day(client, v3_target_path: Path) -> date:
    """The newest 数据日期 in the V3 overview table: the sync writes final days only."""
    table_id = load_json_object(v3_target_path)["tables"]["overview_daily"]
    days = [str(row.get("数据日期") or "")[:10] for _, row in client.list_records(table_id)]
    days = [day for day in days if day]
    if not days:
        raise ValueError("V3 overview table has no finalized days")
    return max(date.fromisoformat(day) for day in days)


def ga4_adapter():
    """Read-only GA4 client over REST, which follows the server's explicit proxy."""
    import google.auth
    from google.analytics.data_v1beta import BetaAnalyticsDataClient

    from website_analytics.adapters.ga4 import GA4Adapter

    credentials, _ = google.auth.default(scopes=("https://www.googleapis.com/auth/analytics.readonly",))
    return GA4Adapter(BetaAnalyticsDataClient(credentials=credentials, transport="rest"))


def fetch_fingerprints(adapter, site, days, mapping, dimension) -> dict:
    rows = adapter.report(site, DateRange(days[0], days[-1]), REPORT_DIMENSIONS)
    metadata = dict(getattr(adapter, "last_response_metadata", {}) or {})
    if metadata.get("sampled"):
        raise SystemExit("GA4 sampled the crawler fingerprint report; not using it")
    return fingerprint_days(rows, days=days, mapping=mapping, page_dimension=dimension, ga4_metadata=metadata)


def main() -> int:
    args = _arguments()
    if not 1 <= args.days_back <= 60:
        raise SystemExit("--days-back must be between 1 and 60")
    _load_env(args.env_file)
    site = require_site(load_sites(args.site_config), args.site)
    if site.inquiry_source is None:
        raise SystemExit("registered read-only route source is required")
    mapping = load_product_mapping(args.mapping, site.site_key)
    information = load_information_mapping(args.information_mapping, site.site_key)
    if mapping is None or information is None:
        raise SystemExit("approved product and information mappings are required")
    dimension = build_page_dimension(
        load_page_classification(args.page_classification, site.site_key),
        create_page_dimension_adapter(site.inquiry_source).query(),
    )
    rules = RuleSet(mapping, dimension, information)

    v3_target = load_feishu_v3_target(args.v3_target)
    last_final = latest_final_day(LarkCLIRecordClient(v3_target.record_client_target()), args.v3_target)
    days = [last_final - timedelta(days=offset) for offset in range(args.days_back - 1, -1, -1)]
    recomputed = recompute_days(
        site=site.site_key, days=days, cache_dir=args.cache_dir, audit_dir=args.audit_dir,
        current=rules, baseline=rules,
    )
    if recomputed["mismatches"]:
        raise SystemExit(f"recompute reported mismatches: {recomputed['mismatches'][:3]}")
    summary: dict = {
        "mode": "apply" if args.apply else "dry_run",
        "last_final": last_final.isoformat(),
        "recomputed_days": len(recomputed["completed_days"]),
        "skipped_days": [item["data_date"] for item in recomputed["skipped_days"]],
    }
    fingerprints = fetch_fingerprints(ga4_adapter(), site, days, mapping, dimension)
    summary["fingerprint_days"] = len(fingerprints)
    if args.apply and recomputed["completed_days"]:
        summary["saved_days"] = len(save_days(args.store, {
            "overview_daily": recomputed["records"]["overview_daily"],
            "product_funnel_daily": recomputed["records"]["product_funnel_daily"],
        }))
    if args.apply:
        save_fingerprint_days(args.crawler_store, fingerprints, datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None))
    after_write = None
    if args.alert_config is not None:
        alert_config = load_alert_config(args.alert_config)

        def after_write(records):
            return send_weekly_alert(
                site=site.site_key, change_rows=records["product_change"], config=alert_config,
                state_path=args.alert_state, send=run_lark, runtime=lark_runtime(), apply=True,
            )

    summary["period_tables"] = refresh_period_tables(
        site=site.site_key,
        mapping=mapping,
        store=args.store,
        exclusions=args.exclusions,
        crawler_store=args.crawler_store,
        crawler_rule=args.crawler_rule,
        crawler_extra=None if args.apply else fingerprints,
        target=args.period_target,
        client_factory=lambda target: LarkCLIRecordClient(target.record_client_target()),
        apply=args.apply,
        after_write=after_write,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
