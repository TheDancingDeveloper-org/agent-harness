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

import re
from collections.abc import Iterable

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
