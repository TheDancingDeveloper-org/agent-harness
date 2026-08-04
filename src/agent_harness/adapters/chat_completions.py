"""The chat-completions wire shape, as served by a great many gateways.

`POST {endpoint}/chat/completions` with `{"model": …, "messages": […]}`, a
bearer credential, the reply under `choices[0].message.content` and token
counts under `usage`. One vendor defined it and dozens of servers now
implement it, which makes it an interoperability protocol worth having a name
for — and still a *specific* format, which is why it is here and not in core.

This preset makes no claim about *failures*: it takes the generic HTTP
classifier, which cannot tell a spend cap from a burst limit because nothing in
HTTP can. A gateway that states the reason in its body pairs this protocol with
a classifier that can read it; `claw_bay` is that pairing.

Reached by name — `preset: chat-completions` — through the entry point this
distribution declares. No core module imports it.
"""

from __future__ import annotations

from ..protocols import BearerAuth, JsonChatRequest, JsonResponseReader, RoutePreset
from ..providers import GENERIC

#: `POST {endpoint}/chat/completions`. The endpoint stays the base URL an
#: operator was given, because that is what every gateway's documentation
#: prints and appending the path here is one fewer thing to get wrong.
REQUEST = JsonChatRequest(name="chat-completions", path="/chat/completions")

#: Both shapes, in order. `choices` is this protocol's own; `content.0.text` is
#: accepted because gateways that translate for a second upstream routinely
#: pass that envelope through unchanged, and reporting an empty answer for a
#: body we can plainly read would be a parsing gap masquerading as a refusal.
READER = JsonResponseReader(
    name="chat-completions",
    text_paths=("choices.0.message.content", "content.0.text"),
)

PRESET = RoutePreset(
    name="chat-completions",
    request=REQUEST,
    auth=BearerAuth(),
    reader=READER,
    classifier=GENERIC,
    summary=(
        "OpenAI-compatible chat completions: POST /chat/completions, bearer "
        "credential, reply under choices[0].message.content. Generic failure "
        "classification -- pair it with a classifier that reads your gateway's "
        "error envelope if it has one."
    ),
)
