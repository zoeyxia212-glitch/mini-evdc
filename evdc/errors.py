"""Error types shared by the platform and the cloud adapters."""


class EvdcError(Exception):
    """Base error for the platform core."""


class NotFound(EvdcError):
    pass


class QuotaExceeded(EvdcError):
    pass


class InvalidState(EvdcError):
    pass


# --- cloud API errors -----------------------------------------------------

class CloudApiError(Exception):
    """Base error raised by provider APIs and adapters."""
    retryable = False

    def __init__(self, message: str, provider: str = "", code: str = ""):
        super().__init__(message)
        self.provider = provider
        self.code = code


class AuthError(CloudApiError):
    """Bad or expired credentials. Not retried blindly; adapters re-auth once."""


class ThrottledError(CloudApiError):
    """Rate limit hit (Aliyun Throttling, AWS RequestLimitExceeded, Azure 429)."""
    retryable = True


class TransientError(CloudApiError):
    """5xx or connection reset: safe to retry."""
    retryable = True


class ApiTimeout(CloudApiError):
    """The call took longer than the configured timeout."""
    retryable = True


def is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, CloudApiError) and exc.retryable
