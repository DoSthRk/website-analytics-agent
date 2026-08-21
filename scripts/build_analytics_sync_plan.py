"""Render a deterministic analytics synchronization plan from a local profile."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from website_analytics.sync_plan import build_sync_plan, load_sync_profile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--anchor", required=True, help="ISO date in the profile timezone")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        anchor = date.fromisoformat(args.anchor)
    except ValueError as error:
        raise ValueError("--anchor must use YYYY-MM-DD") from error
    result = build_sync_plan(load_sync_profile(args.profile), anchor)
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "ok",
                "site": result["site"],
                "anchor": result["anchor"],
                "periods": len(result["periods"]),
                "output": str(args.output) if args.output is not None else None,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
