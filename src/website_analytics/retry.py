"""Bounded, deterministic retries for approved analytics reads."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from google.api_core import exceptions as google_exceptions


_Result = TypeVar("_Result")
_Sleep = Callable[[float], None]


class _RetryPolicy(Protocol):
    max_attempts: int
    initial_delay_seconds: float
    max_delay_seconds: float


@dataclass(frozen=True)
class RetryPolicy:
    """Fixed retry limits with deterministic, bounded exponential backoff."""

    max_attempts: int = 3
    initial_delay_seconds: float = 0.25
    max_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must not be negative")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("max_delay_seconds must not be less than initial_delay_seconds")


DEFAULT_RETRY_POLICY = RetryPolicy()


def retry_transient(
    operation: Callable[[], _Result],
    *,
    policy: _RetryPolicy = DEFAULT_RETRY_POLICY,
    sleep: _Sleep = time.sleep,
) -> _Result:
    """Run an approved read, retrying only bounded transient failures."""
    for attempt in range(policy.max_attempts):
        try:
            return operation()
        except Exception as error:
            if not is_transient_error(error) or attempt + 1 >= policy.max_attempts:
                raise
            sleep(min(policy.initial_delay_seconds * (2**attempt), policy.max_delay_seconds))
    raise AssertionError("retry loop must return or raise")


def is_transient_error(error: Exception) -> bool:
    """Recognize transport, timeout, 429, and server errors only."""
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    if isinstance(
        error,
        (
            google_exceptions.TooManyRequests,
            google_exceptions.ServiceUnavailable,
            google_exceptions.InternalServerError,
            google_exceptions.GatewayTimeout,
            google_exceptions.DeadlineExceeded,
        ),
    ):
        return True
    status = _http_status(error)
    return status == 429 or status is not None and 500 <= status <= 599


def _http_status(error: Exception) -> int | None:
    for value in (
        getattr(error, "status_code", None),
        getattr(error, "code", None),
        getattr(getattr(error, "resp", None), "status", None),
    ):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None
