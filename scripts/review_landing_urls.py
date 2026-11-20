"""Check the actual GA4 landing paths behind a GeneMedi URL review.

Pipe the review's URL column (one path per line) to this command. Output is JSON.
Example: PYTHONPATH=src python scripts/review_landing_urls.py --start 2026-09-21
         --end 2026-11-15 --cache-dir /opt/website-analytics/shared/cache < paths.txt
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.client import HTTPConnection, HTTPSConnection, HTTPException
import json
from pathlib import Path
import sys
from urllib.parse import quote, urljoin, urlsplit

from website_analytics.dashboard_sync import cache_path_for_period
from website_analytics.page_classification import canonical_page_path
from website_analytics.periods import AnalyticsPeriod


ORIGIN = "https://www.genemedi.net"
SITE_HOSTS = {"genemedi.net", "www.genemedi.net"}
REDIRECTS = {301, 302, 303, 307, 308}


def collect_variants(keys, cache_dir, start, end):
    counts = Counter()
    missing_days = []
    day = start
    while day <= end:
        period = AnalyticsPeriod("day", day, day)
        cache = cache_path_for_period(cache_dir, "genemedi-net", "ga4", period)
        if cache.is_file():
            for row in json.loads(cache.read_text())["GA4 Pages"]:
                value = row["landingPagePlusQueryString"]
                if not isinstance(value, str) or not value.startswith("/"):
                    continue
                # GA4 gives a path, so a leading double slash belongs to the request target.
                path = value.partition("?")[0].partition("#")[0]
                key = canonical_page_path(value)
                if key in keys and row["sessions"] > 0:
                    counts[key, path] += row["sessions"]
        else:
            missing_days.append(day.isoformat())
        day += timedelta(days=1)
    return counts, missing_days


def request_status(url):
    parts = urlsplit(url)
    connection = (HTTPSConnection if parts.scheme == "https" else HTTPConnection)(
        parts.hostname, timeout=15
    )
    try:
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parts.query:
            path += "?" + quote(parts.query, safe="=&%/+!$'()*,:;@?-._~")
        connection.request("GET", path, headers={"User-Agent": "GeneMedi-URL-Review/1.0"})
        response = connection.getresponse()
        return response.status, response.getheader("Location")
    finally:
        connection.close()


def probe(path):
    url = ORIGIN + path
    hops = []
    try:
        for _ in range(8):
            parts = urlsplit(url)
            allowed = parts.scheme in {"https", "http"} and parts.netloc in SITE_HOSTS
            if not allowed:
                return {"hops": hops, "result": "external_redirect"}
            status, location = request_status(url)
            hops.append({"path": parts.path, "status": status})
            if status not in REDIRECTS or not location:
                if status == 404:
                    result = "404"
                elif 200 <= status < 300:
                    result = "redirect_ok" if len(hops) > 1 else "ok"
                else:
                    result = "unverified"
                return {"hops": hops, "result": result}
            url = urljoin(url, location)
    except (HTTPException, OSError, ValueError) as error:
        return {"hops": hops, "result": "unverified", "error": type(error).__name__}
    return {"hops": hops, "result": "unverified", "error": "redirect_limit"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    args = parser.parse_args()
    keys = {canonical_page_path(line.strip()) for line in sys.stdin if line.strip()}
    if not keys or args.end < args.start:
        parser.error("supply at least one path and an inclusive date range")
    counts, missing = collect_variants(keys, args.cache_dir, args.start, args.end)
    pairs = sorted(counts)
    with ThreadPoolExecutor(max_workers=4) as pool:
        checks = pool.map(probe, (path for key, path in pairs))
        rows = [
            {"key": key, "path": path, "sessions": counts[key, path], **check}
            for (key, path), check in zip(pairs, checks)
        ]
    json.dump({
        "start": args.start.isoformat(), "end": args.end.isoformat(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "missing_cache_days": missing, "missing_keys": sorted(keys - {key for key, path in pairs}),
        "rows": rows,
    }, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
