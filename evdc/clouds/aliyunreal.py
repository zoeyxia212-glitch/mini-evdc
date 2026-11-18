"""Real Alibaba Cloud client for the AliyunAdapter, built on the official SDKs:

    pip install alibabacloud_ecs20140526 alibabacloud_vpc20160428 alibabacloud_tea_openapi
    (or: pip install ".[aliyun]")

The adapter calls PascalCase methods with PascalCase arguments (exactly the
OpenAPI names, e.g. describe_instances(RegionId=..., PageNumber=...)) and
expects the raw PascalCase response body. This wrapper:

- builds the SDK request model (DescribeInstancesRequest(region_id=..., ...)),
- calls the ECS or VPC client (DescribeVpcs / DescribeEipAddresses live in VPC),
- returns response.body.to_map(), which is the PascalCase dict the adapter maps,
- translates SDK errors (TeaException: .code, .message, .data["statusCode"])
  into Mini EvDC's error types so the retry policy knows what to do.
"""
from __future__ import annotations

import re
from typing import Any

from ..errors import AuthError, CloudApiError, IncorrectState, ResourceNotFound, ThrottledError, TransientError

# OpenAPI action -> (which client, request model name, client method)
OPERATIONS = {
    "describe_instances": ("ecs", "DescribeInstancesRequest", "describe_instances"),
    "describe_disks": ("ecs", "DescribeDisksRequest", "describe_disks"),
    "start_instance": ("ecs", "StartInstanceRequest", "start_instance"),
    "stop_instance": ("ecs", "StopInstanceRequest", "stop_instance"),
    "reboot_instance": ("ecs", "RebootInstanceRequest", "reboot_instance"),
    "describe_vpcs": ("vpc", "DescribeVpcsRequest", "describe_vpcs"),
    "describe_eip_addresses": ("vpc", "DescribeEipAddressesRequest", "describe_eip_addresses"),
}

THROTTLE_PREFIXES = ("Throttling",)  # Throttling, Throttling.User, Throttling.Api ...
AUTH_CODES = {"InvalidAccessKeyId.NotFound", "InvalidAccessKeyId", "SignatureDoesNotMatch",
              "Forbidden.RAM", "Forbidden", "IncompleteSignature", "InvalidSecurityToken.Expired"}
STATE_CODES = {"IncorrectInstanceStatus", "IncorrectInstanceStatus.Initializing", "IncorrectStatus"}
TRANSIENT_CODES = {"ServiceUnavailable", "InternalError", "UnknownError", "OperationConflict"}


def snake(name: str) -> str:
    """RegionId -> region_id, InstanceIds -> instance_ids."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def translate(exc: Exception) -> Exception:
    """Map an SDK exception to a CloudApiError (duck-typed on TeaException's attributes)."""
    if isinstance(exc, CloudApiError):
        return exc
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        msg = getattr(exc, "message", None) or str(exc)
        data = getattr(exc, "data", None) or {}
        status = int(data.get("statusCode") or getattr(exc, "statusCode", 0) or 0)
        if code.startswith(THROTTLE_PREFIXES):
            return ThrottledError(msg, "aliyun", code)
        if code in AUTH_CODES:
            return AuthError(msg, "aliyun", code)
        if code.endswith(".NotFound") or code.startswith("InvalidInstanceId"):
            return ResourceNotFound(msg, "aliyun", code)
        if code in STATE_CODES:
            return IncorrectState(msg, "aliyun", code)
        if code in TRANSIENT_CODES or status >= 500:
            return TransientError(msg, "aliyun", code)
        return CloudApiError(msg, "aliyun", code)
    if type(exc).__name__ in {"ConnectTimeout", "ReadTimeout", "ConnectionError", "RetryError"}:
        return TransientError(str(exc), "aliyun", type(exc).__name__)
    return exc


class AliyunClient:
    """Drop-in replacement for FakeAliyunEcs that talks to the real Alibaba Cloud APIs."""

    def __init__(self, credentials: dict | None = None, *, ecs: Any = None, vpc: Any = None,
                 ecs_models: Any = None, vpc_models: Any = None):
        credentials = credentials or {}
        region = credentials.get("region", "cn-hangzhou")
        if ecs is None or vpc is None:
            try:
                from alibabacloud_ecs20140526 import models as ecs_models_mod
                from alibabacloud_ecs20140526.client import Client as EcsClient
                from alibabacloud_tea_openapi import models as open_api_models
                from alibabacloud_vpc20160428 import models as vpc_models_mod
                from alibabacloud_vpc20160428.client import Client as VpcClient
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise RuntimeError('Alibaba Cloud SDK not installed: pip install ".[aliyun]"') from exc

            def config(endpoint: str):
                return open_api_models.Config(
                    access_key_id=credentials["access_key_id"], access_key_secret=credentials["access_key_secret"],
                    region_id=region, endpoint=endpoint, connect_timeout=5000, read_timeout=15000)

            ecs = EcsClient(config(f"ecs.{region}.aliyuncs.com"))
            vpc = VpcClient(config(f"vpc.{region}.aliyuncs.com"))
            ecs_models, vpc_models = ecs_models_mod, vpc_models_mod
        self._clients = {"ecs": ecs, "vpc": vpc}
        self._models = {"ecs": ecs_models, "vpc": vpc_models}

    def __getattr__(self, op: str):
        if op not in OPERATIONS:
            raise AttributeError(op)
        which, request_name, method = OPERATIONS[op]

        def call(**kwargs):
            request = getattr(self._models[which], request_name)(**{snake(k): v for k, v in kwargs.items()})
            try:
                response = getattr(self._clients[which], method)(request)
            except Exception as exc:  # noqa: BLE001 - translated or re-raised as is
                raise translate(exc) from exc
            return response.body.to_map()
        return call


def factory(credentials: dict) -> AliyunClient:
    return AliyunClient(credentials)
