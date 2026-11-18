"""Real AWS EC2 client for the AwsAdapter, built on boto3.

The fake EC2 client (clouds/fake.py) already uses boto3's method names and
response shapes, so the real client is a thin wrapper whose only job is to
translate boto3 / botocore exceptions into Mini EvDC's error types, which tell
the retry policy and the API what to do:

    RequestLimitExceeded, Throttling ... -> ThrottledError   (retried with backoff)
    AuthFailure, UnauthorizedOperation   -> AuthError        (not retried)
    *.NotFound                           -> ResourceNotFound (HTTP 404)
    IncorrectInstanceState               -> IncorrectState   (HTTP 409)
    5xx, connection / read timeouts      -> TransientError   (retried)

Install the extra first:  pip install "mini-evdc[aws]"   (or: pip install boto3)
Credentials: pass access_key / secret_key in the account config (as env:VAR),
or leave them out to use boto3's default chain (AWS_PROFILE, ~/.aws/credentials,
environment variables, or an instance role).
"""
from __future__ import annotations

from typing import Any, Callable

from ..errors import (AuthError, CloudApiError, IncorrectState, ResourceNotFound, ThrottledError,
                      TransientError)

THROTTLE_CODES = {"RequestLimitExceeded", "Throttling", "ThrottlingException", "TooManyRequestsException"}
AUTH_CODES = {"AuthFailure", "UnauthorizedOperation", "InvalidClientTokenId", "SignatureDoesNotMatch",
              "ExpiredToken", "AccessDenied", "AccessDeniedException"}
TRANSIENT_EXC_NAMES = {"EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
                       "ConnectionClosedError", "ConnectionError"}

# methods the AwsAdapter calls; anything else is refused so the wrapper stays read/start/stop only
ALLOWED = {"describe_instances", "describe_instance_types", "describe_volumes", "describe_vpcs",
           "describe_addresses", "start_instances", "stop_instances", "reboot_instances"}


def translate(exc: Exception) -> Exception:
    """Map a boto3 / botocore exception to a CloudApiError (duck-typed, so it is testable without boto3)."""
    if isinstance(exc, CloudApiError):
        return exc
    response = getattr(exc, "response", None)
    if isinstance(response, dict) and "Error" in response:
        code = response["Error"].get("Code", "")
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        msg = response["Error"].get("Message", str(exc))
        if code in THROTTLE_CODES:
            return ThrottledError(msg, "aws", code)
        if code in AUTH_CODES:
            return AuthError(msg, "aws", code)
        if code.endswith(".NotFound"):
            return ResourceNotFound(msg, "aws", code)
        if code == "IncorrectInstanceState":
            return IncorrectState(msg, "aws", code)
        if status >= 500 or code in ("InternalError", "ServiceUnavailable", "Unavailable"):
            return TransientError(msg, "aws", code)
        return CloudApiError(msg, "aws", code)  # 4xx we do not understand: surface it, do not retry
    if type(exc).__name__ in TRANSIENT_EXC_NAMES:
        return TransientError(str(exc), "aws", type(exc).__name__)
    return exc


class Ec2Client:
    """Drop-in replacement for FakeEc2 that talks to the real EC2 API."""

    def __init__(self, credentials: dict | None = None, ec2: Any = None):
        credentials = credentials or {}
        self.region = credentials.get("region", "ap-southeast-2")
        if ec2 is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise RuntimeError('boto3 is not installed: pip install "mini-evdc[aws]"') from exc
            kwargs = {"region_name": self.region,
                      # the adapter already retries; keep boto's own retries short so the two don't multiply
                      "config": Config(retries={"max_attempts": 2, "mode": "standard"},
                                       connect_timeout=5, read_timeout=15)}
            if credentials.get("access_key") and credentials.get("secret_key"):
                kwargs["aws_access_key_id"] = credentials["access_key"]
                kwargs["aws_secret_access_key"] = credentials["secret_key"]
            if credentials.get("profile"):
                ec2 = boto3.Session(profile_name=credentials["profile"]).client("ec2", **kwargs)
            else:
                ec2 = boto3.client("ec2", **kwargs)
        self._ec2 = ec2

    def __getattr__(self, method: str) -> Callable[..., dict]:
        if method not in ALLOWED:
            raise AttributeError(method)
        fn = getattr(self._ec2, method)

        def call(**kwargs):
            try:
                return fn(**kwargs)
            except Exception as exc:  # noqa: BLE001 - translated or re-raised as is
                raise translate(exc) from exc
        return call


def factory(credentials: dict) -> Ec2Client:
    return Ec2Client(credentials)
