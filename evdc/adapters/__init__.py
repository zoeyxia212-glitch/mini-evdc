"""Importing this package registers every adapter in ADAPTERS."""
from . import aliyun, aws, azure, local  # noqa: F401  (register on import)
from .base import ADAPTERS, ProviderAdapter, register

__all__ = ["ADAPTERS", "ProviderAdapter", "register"]
