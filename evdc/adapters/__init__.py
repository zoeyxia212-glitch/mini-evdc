"""Importing this package registers every adapter in ADAPTERS."""
from .base import ADAPTERS, ProviderAdapter, register
from . import aliyun, aws, azure, local  # noqa: F401  (register on import)

__all__ = ["ADAPTERS", "ProviderAdapter", "register"]
