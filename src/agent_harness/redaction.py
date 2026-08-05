"""Removing credentials from text before anything durable sees it.

The event store is append-only and the audit store has no mutation surface.
That is deliberate, and it is exactly why this exists: a credential written
into either cannot be deleted afterwards, only rotated. So the filter belongs
at the boundary *before* the first write, not at the read edge where the
plaintext is already on disk.

Two sources of knowledge, and the first is worth far more than the second:

- **Values this deployment knows it holds** — the API key a route was
  configured with, the service token, anything an operator hands over. Exact
  string replacement, no guessing, no false negatives.
- **Shapes that are credentials wherever they appear** — a bearer header, an
  assignment to something named like a key. These catch what the deployment
  was never told about, at the cost of occasionally redacting something
  harmless.

**This is a reduction, not a guarantee**, and nothing here should be described
as one. It cannot catch a credential whose shape it does not know and whose
value it was not given. It removes the routine cases — an agent echoing an
environment variable, a provider quoting the header it rejected, a check
command printing a failing request — which is most of them.

Nothing here names a vendor. A pattern keyed to one provider's key prefix
would be core knowing what a particular vendor is called, which is the one
thing this repository rules out.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance only
    from .events import Event

log = logging.getLogger(__name__)

#: What replaces a redacted value. Deliberately visible: a reader must be able
#: to tell "a secret was removed here" from "the model said nothing", because
#: silently shortening text would make the record lie in a way that is
#: indistinguishable from the truth.
MARK = "[redacted]"

#: The shortest value worth replacing. Below this, exact-match redaction does
#: more harm than good: a two-character "key" appears inside ordinary words,
#: and redacting every occurrence would corrupt the text while protecting
#: nothing that was secret in the first place.
MIN_SECRET_LEN = 8

#: Set in an event's `data` when this module removed something from it. The
#: mark inside the text says *where*; this says *that it happened at all*, so
#: a reader of the audit is never left comparing a record against what an
#: agent said and finding a silent difference.
REDACTED_FLAG = "redacted"

#: Set instead when redacting the payload raised. The event is still written
#: -- an event that never lands is indistinguishable from a call that never
#: happened, which is a worse record than a lossy one -- but its payload is
#: dropped rather than passed through unexamined. See `redact_event`.
REDACTION_FAILED = "redaction_failed"

#: Credential *shapes*, for values this deployment was never told about.
#: Each keeps its label and replaces only the value, so the record still says
#: what kind of thing was removed.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # `Authorization: Bearer <token>` and bare `Bearer <token>`.
    ("bearer", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._\-+/=]{8,})")),
    # `api_key=...`, `"apiKey": "..."`, `token: ...`, `secret = ...`.
    (
        "assignment",
        re.compile(
            r"(?i)\b([a-z0-9_\-]*(?:api[_\-]?key|token|secret|password|passwd))\b"
            r"(\"?\s*[:=]\s*\"?)([^\s\"',;}\)]{8,})"
        ),
    ),
)


class Redactor:
    """Removes known credential values and credential-shaped text.

    Callable so it can be passed wherever a `str -> str` is wanted, including
    as the sink between a model's answer and the store.
    """

    def __init__(self, secrets: Iterable[str] = (), *, patterns: bool = True) -> None:
        # Longest first, so a key that contains another key as a prefix does
        # not leave the remainder of the longer one exposed.
        self.secrets = sorted(
            {s.strip() for s in secrets if s and len(s.strip()) >= MIN_SECRET_LEN},
            key=len,
            reverse=True,
        )
        self.patterns = patterns

    def __call__(self, text: str | None) -> str | None:
        """Redact `text`. `None` in, `None` out — absence is not a secret."""
        if not text:
            return text
        for secret in self.secrets:
            text = text.replace(secret, MARK)
        if not self.patterns:
            return text
        text = PATTERNS[0][1].sub(f"Bearer {MARK}", text)
        text = PATTERNS[1][1].sub(rf"\1\2{MARK}", text)
        return text

    def redacted(self, text: str | None) -> bool:
        """Whether calling this on `text` would remove anything.

        Used to record *that* a redaction happened, so the audit does not
        silently differ from what the model actually said.
        """
        return bool(text) and self(text) != text


#: What a store hands this module: `str | None -> str | None`.
Redact = Callable[[str | None], str | None]


def redact_text(value: Any, redact: Redact) -> Any:
    """Redact every string reachable inside `value`, structure preserved.

    Walks lists, tuples and dicts because an event's payload is arbitrary
    JSON: the credential-carrying cases -- an agent's output, a check
    command's stderr, a provider's error envelope -- arrive nested about as
    often as they arrive at the top level.

    Dictionary *keys* are left alone. A key is a field name chosen by the
    writer, not text a credential arrives in, and rewriting keys would change
    the shape of the record every reader is written against.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_text(v, redact) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_text(v, redact) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_text(v, redact) for v in value)
    return value


def redact_payload(
    data: dict[str, Any], endpoint: str | None, redact: Redact
) -> tuple[dict[str, Any], str | None, bool]:
    """Redact the two parts of a record a credential can actually reach.

    `data` is where every free-text payload lives -- an agent's output, a
    check command's stderr, a provider's error envelope, a diff. `endpoint`
    is a URL, and a URL can carry a key in a query string. The remaining
    columns are drawn from fixed vocabularies -- a kind, an outcome, an error
    class, a role -- and are what the gates and the panels count. Nothing a
    credential can reach arrives in them, and rewriting them would put this
    module in a position to change a measured number, which it must never be
    in.

    Returns `(data, endpoint, changed)`. Never raises: observation must not
    stop work. If redaction fails, the payload is dropped and marked rather
    than passed through, because the store cannot unwrite what it has taken
    -- a deliberate trade of detail for exposure, and a visible one.
    """
    try:
        clean = redact_text(data, redact)
        clean_endpoint = redact(endpoint)
        if clean == data and clean_endpoint == endpoint:
            return data, endpoint, False
        return {**clean, REDACTED_FLAG: True}, clean_endpoint, True
    except Exception as exc:  # noqa: BLE001 - a bug here must not lose the record
        log.warning("redaction failed; payload dropped rather than written: %s", exc)
        return {REDACTED_FLAG: True, REDACTION_FAILED: True}, endpoint, True


def redact_event(event: Event, redact: Redact) -> Event:
    """The one thing a store does to an event before writing it.

    The event itself always survives, columns intact: an event that never
    lands is indistinguishable from a call that never happened, which is a
    worse record than a lossy one.
    """
    data, endpoint, changed = redact_payload(event.data, event.endpoint, redact)
    if not changed:
        return event
    return replace(event, data=data, endpoint=endpoint)


def from_environment(*extra: str | None) -> Redactor:
    """A redactor holding this process's own credentials.

    Reads the values the harness itself is configured with. They are the ones
    most likely to be echoed back by an agent or quoted by a provider, and the
    ones whose exposure would be entirely self-inflicted.
    """
    import os

    names = (
        "HARNESS_API_KEY",
        "HARNESS_TOKEN",
        "AIDEVENV_TOKEN",
    )
    values = [os.environ.get(name, "") for name in names]
    values.extend(e or "" for e in extra)
    return Redactor(values)
