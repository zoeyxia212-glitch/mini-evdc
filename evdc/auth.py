"""API keys and role-based access control.

Roles (modelled on the EvDC / vCloud Director tenant roles):

    system_admin  the provider (VNET): everything, including multi-cloud sync
                  and actions on public-cloud resources
    org_admin     one tenant org: full control of that org's VDCs, networks,
                  VMs, templates, rules and users; read-only multi-cloud inventory
    viewer        one tenant org: read-only (portal "console access only")

Keys are random tokens shown once; only their SHA-256 hash is stored.
Clients send them as `Authorization: Bearer <key>` or `X-API-Key: <key>`.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass

from .errors import EvdcError
from .store import Store

ROLES = ("system_admin", "org_admin", "viewer")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")


class Unauthorized(EvdcError):
    """No key or an unknown key (HTTP 401)."""


class Forbidden(EvdcError):
    """Key is valid but the role or org scope does not allow this (HTTP 403)."""


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    org_id: str | None   # None for system_admin

    @property
    def is_system(self) -> bool:
        return self.role == "system_admin"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_key() -> str:
    return "evdc_" + secrets.token_urlsafe(24)


class AuthService:
    def __init__(self, store: Store):
        self.store = store

    def create_user(self, name: str, role: str, org_id: str | None, key: str | None = None) -> str:
        """Create a user and return its API key (only time it is visible)."""
        if not _NAME_RE.match(name):
            raise EvdcError("user name: 2-32 chars, lowercase letters, digits, . _ -")
        if role not in ROLES:
            raise EvdcError(f"role must be one of {ROLES}")
        if (role == "system_admin") != (org_id is None):
            raise EvdcError("system_admin has no org; org_admin and viewer need an org")
        if any(u["name"] == name for u in self.store.list_users()):
            raise EvdcError(f"user {name} already exists")
        key = key or generate_key()
        self.store.add_user(name, hash_key(key), role, org_id)
        return key

    def authenticate(self, key: str | None) -> Principal:
        if not key:
            raise Unauthorized("missing API key (Authorization: Bearer <key> or X-API-Key)")
        # looked up by SHA-256 hash, so the raw key is never compared or stored
        user = self.store.user_by_hash(hash_key(key))
        if not user:
            raise Unauthorized("invalid API key")
        return Principal(user["name"], user["role"], user["org_id"])

    @staticmethod
    def require(p: Principal, *, write: bool = False, org_id: str | None = None, system: bool = False) -> None:
        """Raise Forbidden unless the principal may do this.

        system=True   only system_admin
        org_id=X      the object belongs to org X; tenant users must belong to X
        write=True    viewers are refused
        """
        if p.is_system:
            return
        if system:
            raise Forbidden("only a system administrator can do this")
        if org_id is not None and p.org_id != org_id:
            raise Forbidden("this object belongs to another org")
        if write and p.role == "viewer":
            raise Forbidden("viewer role is read-only")
