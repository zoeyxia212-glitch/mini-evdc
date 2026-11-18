"""Timeout and retry handling for cloud API calls.

- Exponential backoff with full jitter (the AWS-recommended strategy).
- Only retries errors marked retryable (throttling, 5xx, timeouts).
- Each call can be wrapped with a timeout so a hanging API does not block a
  whole sync run.
"""
from __future__ import annotations

import concurrent.futures
import functools
import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from .errors import ApiTimeout, AuthError, is_retryable
from .logs import get_logger

T = TypeVar("T")
log = get_logger("evdc.retry")

# One shared pool so timed calls do not create a thread per request.
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="cloudcall")


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5     # seconds
    max_delay: float = 8.0
    timeout: float | None = 10.0  # per attempt; None = no timeout

    def backoff(self, attempt: int) -> float:
        """Full jitter: random between 0 and min(max_delay, base * 2^attempt)."""
        cap = min(self.max_delay, self.base_delay * (2 ** attempt))
        return random.uniform(0, cap)


def call_with_timeout(fn: Callable[[], T], timeout: float | None) -> T:
    if timeout is None:
        return fn()
    future = _pool.submit(fn)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise ApiTimeout(f"call exceeded {timeout}s timeout") from None


def run_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    op: str,
    ctx: dict | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run fn with timeout + retry. `sleep` is injectable so tests run instantly."""
    ctx = dict(ctx or {}, op=op)
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return call_with_timeout(fn, policy.timeout)
        except Exception as exc:  # noqa: BLE001 - we re-raise below
            last = attempt == policy.max_attempts
            if not is_retryable(exc) or last:
                # auth errors are handled one level up (re-auth), so only warn
                level = log.warning if isinstance(exc, AuthError) else log.error
                level("call failed", extra={"ctx": {**ctx, "attempt": attempt,
                                                    "error_type": type(exc).__name__, "error": str(exc),
                                                    "retryable": is_retryable(exc)}})
                raise
            delay = policy.backoff(attempt)
            log.warning("retrying", extra={"ctx": {**ctx, "attempt": attempt,
                                                   "error_type": type(exc).__name__,
                                                   "error": str(exc), "sleep_s": round(delay, 3)}})
            sleep(delay)
    raise RuntimeError("unreachable")


def retrying(policy: RetryPolicy | None = None, op: str | None = None):
    """Decorator form of run_with_retry."""
    policy = policy or RetryPolicy()

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return run_with_retry(lambda: fn(*args, **kwargs), policy, op=op or fn.__name__)
        return wrapper
    return deco
