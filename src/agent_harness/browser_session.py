"""Opaque browser sessions and CSRF protection for the first-party GUI."""

from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class BrowserSession:
    session_id: str
    csrf_token: str
    operator: str
    expires_at: float
    token_fingerprint: str


@dataclass(frozen=True)
class BrowserReview:
    """One exact, short-lived browser action awaiting explicit confirmation."""

    review_id: str
    session_id: str
    kind: str
    target_id: str
    baseline_digest: str
    baseline_version: float
    payload: dict[str, Any]
    expires_at: float


class BrowserSessions:
    """Process-local bounded sessions.

    The service's configured bearer token is the credential exchange input, not
    a browser credential. Restarting this process revokes all sessions by
    design; deployments needing continuity can add a protected server-side
    store later without changing the browser contract.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 8 * 60 * 60,
        max_sessions: int = 256,
        review_ttl_seconds: int = 10 * 60,
        max_reviews: int = 512,
    ) -> None:
        if ttl_seconds <= 0 or max_sessions <= 0 or review_ttl_seconds <= 0 or max_reviews <= 0:
            raise ValueError("session limits must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self.review_ttl_seconds = review_ttl_seconds
        self.max_reviews = max_reviews
        self._sessions: dict[str, BrowserSession] = {}
        self._reviews: dict[str, BrowserReview] = {}
        self._login_failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow_login(self, client_key: str, *, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        with self._lock:
            failures = self._login_failures[client_key]
            while failures and failures[0] <= moment - 60:
                failures.popleft()
            return len(failures) < 5

    def record_login_failure(self, client_key: str, *, now: float | None = None) -> None:
        moment = time.time() if now is None else now
        with self._lock:
            failures = self._login_failures[client_key]
            while failures and failures[0] <= moment - 60:
                failures.popleft()
            failures.append(moment)

    def create(
        self,
        operator: str = "operator",
        *,
        token_fingerprint: str = "",
        now: float | None = None,
    ) -> BrowserSession:
        moment = time.time() if now is None else now
        session = BrowserSession(
            session_id=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            operator=operator,
            expires_at=moment + self.ttl_seconds,
            token_fingerprint=token_fingerprint,
        )
        with self._lock:
            self._purge(moment)
            if len(self._sessions) >= self.max_sessions:
                oldest = min(self._sessions.values(), key=lambda item: item.expires_at)
                self._sessions.pop(oldest.session_id, None)
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str | None, *, now: float | None = None) -> BrowserSession | None:
        if not session_id:
            return None
        moment = time.time() if now is None else now
        with self._lock:
            self._purge(moment)
            return self._sessions.get(session_id)

    def revoke(self, session_id: str | None) -> None:
        if session_id:
            with self._lock:
                self._sessions.pop(session_id, None)
                for review_id in [
                    key for key, review in self._reviews.items() if review.session_id == session_id
                ]:
                    self._reviews.pop(review_id, None)

    def create_review(
        self,
        session: BrowserSession,
        *,
        kind: str,
        target_id: str,
        baseline_digest: str,
        baseline_version: float,
        payload: dict[str, Any],
        now: float | None = None,
    ) -> BrowserReview:
        moment = time.time() if now is None else now
        review = BrowserReview(
            review_id=secrets.token_urlsafe(32),
            session_id=session.session_id,
            kind=kind,
            target_id=target_id,
            baseline_digest=baseline_digest,
            baseline_version=baseline_version,
            payload=payload,
            expires_at=moment + self.review_ttl_seconds,
        )
        with self._lock:
            self._purge(moment)
            if len(self._reviews) >= self.max_reviews:
                oldest = min(self._reviews.values(), key=lambda item: item.expires_at)
                self._reviews.pop(oldest.review_id, None)
            self._reviews[review.review_id] = review
        return review

    def consume_review(
        self,
        session: BrowserSession,
        review_id: str,
        *,
        kind: str,
        target_id: str,
        now: float | None = None,
    ) -> BrowserReview:
        """Consume one matching review; replay and cross-session use are refused."""
        moment = time.time() if now is None else now
        with self._lock:
            self._purge(moment)
            review = self._reviews.get(review_id)
            if (
                review is None
                or review.session_id != session.session_id
                or review.kind != kind
                or review.target_id != target_id
            ):
                raise HTTPException(
                    status_code=409, detail="configuration review is invalid or expired"
                )
            self._reviews.pop(review_id, None)
        return review

    def require(self, request: Request) -> BrowserSession:
        session = self.get(request.cookies.get("harness_session"))
        if session is None:
            raise HTTPException(status_code=401, detail="browser session required")
        expected = request.app.state.token or ""
        if not secrets.compare_digest(session.token_fingerprint, self.fingerprint(expected)):
            self.revoke(session.session_id)
            raise HTTPException(status_code=401, detail="browser session was revoked")
        return session

    def require_csrf(
        self, request: Request, session: BrowserSession, submitted: str | None = None
    ) -> None:
        # Form parsing belongs to the action route; this method checks the
        # header only so a missing token cannot be confused with an empty form.
        token = request.headers.get("X-CSRF-Token", "") or (submitted or "")
        origin = request.headers.get("Origin") or request.headers.get("Referer", "")
        request_url = urlsplit(str(request.base_url))
        if not token or not secrets.compare_digest(token, session.csrf_token):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        if origin:
            submitted_url = urlsplit(origin)
            if (
                submitted_url.scheme != request_url.scheme
                or submitted_url.netloc != request_url.netloc
            ):
                raise HTTPException(status_code=403, detail="request origin is not this service")

    @staticmethod
    def fingerprint(token: str) -> str:
        """Bind sessions to the configured token without retaining that token."""
        import hashlib

        return hashlib.sha256(token.encode()).hexdigest()

    def _purge(self, now: float) -> None:
        expired = [key for key, value in self._sessions.items() if value.expires_at <= now]
        for key in expired:
            self._sessions.pop(key, None)
        expired_reviews = [key for key, value in self._reviews.items() if value.expires_at <= now]
        for key in expired_reviews:
            self._reviews.pop(key, None)
