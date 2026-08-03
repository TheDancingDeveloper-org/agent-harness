"""Short-lived credentials scoped to one project, item and attempt.

The coordination plane hands a credential to every agent it starts, and
those agents are running model output in a terminal. The operator's API
token is the wrong thing to give them: it is long-lived, it is the same one
everywhere, and it can do anything the operator can — including reading and
writing another project's rooms.

A scoped token says who the bearer is and what it may touch, and expires.
Three properties follow, and each is enforced at the boundary rather than
trusted:

**Identity is not a claim the caller makes.** A message's sender is taken
from the token, never from the request body. An agent cannot post as the
oversight actor or as another agent, however it is prompted.

**Scope is not advisory.** A token issued for project `alpha` is rejected
on `beta`'s rooms, so a confused or hostile agent cannot reach across the
fleet even though one service serves both.

**Expiry is real.** An attempt that ended hours ago holds a credential that
no longer works, so a leaked token has a bounded blast radius.

This is deliberately a signed value rather than a database row: verification
must not need a round trip on the hot path, and a token nobody stored cannot
be stolen from storage. The cost is that revocation waits for expiry, which
is why the lifetimes are short.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

#: Roles the plane understands. `operator` is a human or the service itself;
#: `oversight` is the per-project coordinator; `agent` is a worker.
OPERATOR = "operator"
OVERSIGHT = "oversight"
AGENT = "agent"
ROLES = frozenset({OPERATOR, OVERSIGHT, AGENT})

#: Default lifetime for an agent's credential. Longer than a long item,
#: short enough that a leaked token is not a standing grant.
DEFAULT_TTL_SECONDS = 6 * 3600


class InvalidScope(ValueError):
    """The token is absent, malformed, forged, expired or out of scope."""


@dataclass(frozen=True)
class Scope:
    """What one credential may do."""

    project_id: str
    agent_id: str
    role: str = AGENT
    item_id: str | None = None
    attempt: int | None = None
    expires_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.project_id.strip():
            raise ValueError("project_id must not be empty")
        if not self.agent_id.strip():
            raise ValueError("agent_id must not be empty")
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}; expected one of {sorted(ROLES)}")

    @property
    def is_operator(self) -> bool:
        return self.role == OPERATOR

    def permits(self, project_id: str) -> bool:
        """An operator credential is fleet-wide; everything else is not."""

        return self.is_operator or self.project_id == project_id

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "agent_id": self.agent_id,
            "role": self.role,
            "item_id": self.item_id,
            "attempt": self.attempt,
            "expires_at": self.expires_at,
        }


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class ScopedTokens:
    """Issues and verifies scoped credentials, using the service's secret.

    The secret is the operator token by default, so a deployment that has
    configured authentication at all has already configured this. There is
    no second thing to set up and therefore no deployment that quietly runs
    without it.
    """

    def __init__(self, secret: str, *, now: Callable[[], float] = time.time) -> None:
        if not secret:
            raise ValueError("a signing secret is required; unsigned tokens are forgeable")
        self.secret = secret.encode()
        self.now = now

    def issue(self, scope: Scope, *, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> str:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        expires_at = scope.expires_at or (self.now() + ttl_seconds)
        payload = dict(scope.as_dict(), expires_at=expires_at)
        body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str) -> Scope:
        try:
            body, signature = token.split(".", 1)
        except ValueError as exc:
            raise InvalidScope("token is malformed") from exc
        # Compared in constant time: a byte-by-byte comparison of a signature
        # leaks, by timing, how much of a guess was right.
        if not hmac.compare_digest(signature, self._sign(body)):
            raise InvalidScope("token signature does not verify")
        try:
            payload = json.loads(_unb64(body))
            scope = Scope(
                project_id=payload["project_id"],
                agent_id=payload["agent_id"],
                role=payload["role"],
                item_id=payload.get("item_id"),
                attempt=payload.get("attempt"),
                expires_at=float(payload["expires_at"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidScope("token payload is not a scope") from exc
        if scope.expires_at <= self.now():
            raise InvalidScope("token has expired")
        return scope

    def _sign(self, body: str) -> str:
        return hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
