import logging

from evdc.retry import RetryPolicy

# keep test output quiet
logging.getLogger("evdc").setLevel(logging.CRITICAL)

FAST = dict(base_delay=0.0, max_delay=0.0, max_attempts=4, timeout=2.0)


def fast_policy(**overrides) -> RetryPolicy:
    return RetryPolicy(**{**FAST, **overrides})


def no_sleep(adapter):
    adapter._sleep = lambda _s: None
    return adapter
