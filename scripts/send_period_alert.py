"""Preview or send the weekly anomaly digest from the daily-facts store.

Rebuilds the period facts locally (no Feishu reads or writes) and prints the
message for the latest complete week. With --apply it sends the message to
the group in the alert config, unless the state file says that week was
already sent. The daily refresh sends the same digest on its own once
--alert-config is passed to run_period_refresh.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from website_analytics.daily_facts_store import last_day, load_days  # noqa: E402
from website_analytics.feishu_tables import lark_runtime, run_lark  # noqa: E402
from website_analytics.period_alerts import load_alert_config, send_weekly_alert, weekly_digest  # noqa: E402
from website_analytics.period_facts import build_period_facts, load_exclusions  # noqa: E402
from website_analytics.product_mapping import load_product_mapping  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="genemedi-net")
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--exclusions", type=Path, required=True)
    parser.add_argument("--alert-config", type=Path, required=True)
    parser.add_argument("--alert-state", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="send the message (default: print it only)")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    mapping = load_product_mapping(args.mapping, args.site)
    if mapping is None:
        raise SystemExit("approved product mapping is required")
    records = load_days(args.store)
    build = build_period_facts(
        site=args.site,
        mapping=mapping,
        funnel_daily=records["product_funnel_daily"],
        overview_daily=records["overview_daily"],
        last_final=last_day(records),
        exclusions=load_exclusions(args.exclusions, args.site),
    )
    config = load_alert_config(args.alert_config)
    digest = weekly_digest(build.records["product_change"], config.dashboard_url)
    if digest is not None and not args.apply:
        print(digest.markdown)
        print()
    result = send_weekly_alert(
        site=args.site, change_rows=build.records["product_change"], config=config,
        state_path=args.alert_state, send=run_lark, runtime=lark_runtime(), apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
