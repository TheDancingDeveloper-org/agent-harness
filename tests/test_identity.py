"""Scoped credentials.

These run against the signing layer directly. The API tests prove the routes
enforce a scope; these prove the scope cannot be forged, widened or outlived
in the first place.
"""

from __future__ import annotations

import base64
import json

import pytest

from agent_harness.identity import (
    AGENT,
    OPERATOR,
    InvalidScope,
    Scope,
    ScopedTokens,
)

SECRET = "service-secret"  # noqa: S105 - a fixture, not a credential


def tokens(now: float = 1000.0) -> ScopedTokens:
    return ScopedTokens(SECRET, now=lambda: now)


def test_a_round_trip_preserves_every_field() -> None:
    scope = Scope(project_id="alpha", agent_id="worker-1", item_id="T7", attempt=3)
    verified = tokens().verify(tokens().issue(scope, ttl_seconds=60))
    assert verified.project_id == "alpha"
    assert verified.agent_id == "worker-1"
    assert verified.item_id == "T7"
    assert verified.attempt == 3
    assert verified.role == AGENT
    assert verified.expires_at == 1060.0


def test_a_token_signed_with_another_secret_is_refused() -> None:
    forged = ScopedTokens("not-the-secret", now=lambda: 1000.0).issue(
        Scope(project_id="alpha", agent_id="worker-1", role=OPERATOR)
    )
    with pytest.raises(InvalidScope, match="signature"):
        tokens().verify(forged)


def test_editing_the_payload_invalidates_the_signature() -> None:
    """The interesting forgery is not a random string, it is a real token
    with `project_id` swapped for somebody else's."""
    token = tokens().issue(Scope(project_id="alpha", agent_id="worker-1"), ttl_seconds=60)
    body, _ = token.split(".", 1)
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["project_id"] = "beta"
    payload["role"] = OPERATOR
    tampered = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    with pytest.raises(InvalidScope):
        tokens().verify(f"{tampered}.{token.split('.', 1)[1]}")


def test_an_expired_token_is_refused_even_though_it_verifies() -> None:
    token = tokens(1000.0).issue(Scope(project_id="alpha", agent_id="w"), ttl_seconds=10)
    assert tokens(1005.0).verify(token).project_id == "alpha"
    with pytest.raises(InvalidScope, match="expired"):
        tokens(1011.0).verify(token)


@pytest.mark.parametrize("token", ["", "nonsense", "no-dot", "a.b", "!!!.???"])
def test_a_malformed_token_is_refused_rather_than_crashing(token: str) -> None:
    with pytest.raises(InvalidScope):
        tokens().verify(token)


def test_an_agent_scope_permits_only_its_own_project() -> None:
    scope = Scope(project_id="alpha", agent_id="worker-1")
    assert scope.permits("alpha") is True
    assert scope.permits("beta") is False


def test_an_operator_scope_is_fleet_wide() -> None:
    scope = Scope(project_id="*", agent_id="operator", role=OPERATOR)
    assert scope.permits("alpha") is True
    assert scope.permits("beta") is True


def test_an_unknown_role_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        Scope(project_id="alpha", agent_id="w", role="superuser")


def test_a_scope_must_name_a_project_and_an_agent() -> None:
    with pytest.raises(ValueError, match="project_id"):
        Scope(project_id="  ", agent_id="w")
    with pytest.raises(ValueError, match="agent_id"):
        Scope(project_id="alpha", agent_id="")


def test_signing_without_a_secret_is_refused() -> None:
    """An unsigned token is a token anybody can write."""
    with pytest.raises(ValueError, match="signing secret"):
        ScopedTokens("")


def test_a_non_positive_lifetime_is_refused() -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        tokens().issue(Scope(project_id="alpha", agent_id="w"), ttl_seconds=0)
