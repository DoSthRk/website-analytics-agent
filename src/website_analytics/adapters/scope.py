"""Bounded page-cohort filters shared by source adapters; no client creation."""
from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import unquote


def page_regex(paths: Sequence[str], domains: Sequence[str] = (), *, exact_case: bool = False) -> str:
    if type(exact_case) is not bool:
        raise ValueError("invalid case policy")
    if isinstance(paths, str) or not 1 <= len(paths) <= 100:
        raise ValueError("page cohort requires 1 to 100 paths")
    for path in paths:
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or any(x in unquote(path) for x in ("?", "#", "\\")) or any(ord(x) < 32 for x in unquote(path)) or any(x in (".", "..") for x in unquote(path).split("/")):
            raise ValueError("invalid page-cohort path")
    alternatives = "|".join(re.escape(p.rstrip("/")) for p in sorted(set(paths)))
    prefix = ""
    if domains:
        if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", d) for d in domains):
            raise ValueError("invalid registered domain")
        prefix = "https?://(?:" + "|".join(re.escape(d) for d in domains) + ")"
    if exact_case and prefix:
        prefix = "(?i:" + prefix + ")"
    result = ("^" if exact_case else "(?i)^") + prefix + "(?:" + alternatives + ")/?(?:\\?.*)?$"
    if len(result) > 4096:
        raise ValueError("page cohort exceeds supported filter size; narrow the batch")
    return result


def validate_segments(countries: Sequence[str], channels: Sequence[str]) -> None:
    if isinstance(countries, str) or len(countries) > 20 or any(not isinstance(x, str) or not re.fullmatch(r"[A-Z]{2}", x) for x in countries):
        raise ValueError("invalid country scope")
    if isinstance(channels, str) or len(channels) > 20 or any(not isinstance(x, str) or not 1 <= len(x) <= 80 or any(ord(c) < 32 for c in x) for x in channels):
        raise ValueError("invalid session channel scope")
