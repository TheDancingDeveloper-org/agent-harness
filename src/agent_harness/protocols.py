"""What a route is made of, and how another one is added without editing core.

A route used to mean two things at once. `Provider` classified failures; the
CLI's transport separately assumed one gateway's request path, one
authentication header and one response envelope. Neither knew about the other,
so "add a vendor" meant editing the transport, and "change the classifier"
changed nothing about the wire.

Those are different abstractions, and this module keeps them apart. A route
names, explicitly or through a **preset**:

- a **request adapter** — what one request looks like on the wire;
- an **authentication strategy** — how the credential is attached, if at all;
- a **response reader** — where the assistant text and the token counts live;
- a **failure classifier** — what a rejection means for control flow;
- a model and an endpoint, which live on the route itself;
- optionally, the name the model is priced under.

The preset shipped here is **generic and claims nothing about any vendor**: a
JSON POST to the endpoint exactly as configured, a bearer credential when one
is supplied, conservative usage reading, and classification from HTTP alone.
Every piece of it is a configured value rather than a literal in a branch, so a
second wire shape is usually a different construction rather than different
code.

Anything that knows a particular gateway's path, envelope or header is an
adapter or a plugin. This module never imports one. It resolves them **by
name**, and loads only the name that was asked for:

- `register()`, for a preset constructed in this process;
- ``HARNESS_ROUTE_PRESETS="name=module:attribute,…"``, for one named in
  configuration;
- an ``agent_harness.route_presets`` entry point, for one shipped by any
  installed distribution — including this one, whose adapters are reached this
  way and by no other route.

Adding a vendor is therefore a new module and a name. It is never an edit to
`model_client.py`.

Nothing here guesses. `suggest()` will look at an endpoint's host and say which
preset is probably meant, but it is a report for a human to act on: resolution
uses the configured name and no other input. A protocol chosen from a hostname
is a protocol nobody configured, and the first sign of it would be a fleet
talking to the wrong URL with the wrong credential.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from . import pricing
from .providers import GENERIC, Provider

log = logging.getLogger(__name__)

#: The entry-point group a distribution declares to publish route presets.
#: Discovery reads the group's *names* without importing anything; only the
#: name a route asks for is ever loaded.
ENTRY_POINT_GROUP = "agent_harness.route_presets"

#: Comma-separated ``name=module:attribute`` pairs, for a preset that is named
#: in configuration rather than published by a package. The attribute is either
#: a `RoutePreset` or a zero-argument callable returning one.
PRESET_PATH_ENV = "HARNESS_ROUTE_PRESETS"


class UnknownPreset(LookupError):
    """A route named a preset nothing declares.

    Raised rather than defaulted: silently substituting a different wire shape
    for the one an operator configured is how a request ends up at the wrong
    URL with the wrong credential, and the only symptom is a failure the
    classifier cannot explain.
    """

    def __init__(self, name: str, known: Sequence[str]) -> None:
        super().__init__(
            f"no route preset named {name!r}; declared: {', '.join(known) or '(none)'}. "
            f"Register one with agent_harness.protocols.register(), name it in "
            f"${PRESET_PATH_ENV}, or install a distribution publishing the "
            f"{ENTRY_POINT_GROUP!r} entry point."
        )
        self.name = name
        self.known = tuple(known)


class RouteLike(Protocol):
    """The part of a route the wire pieces are allowed to see.

    Structural on purpose: `model_client.Route` satisfies it without this
    module importing it, which is what keeps the dependency pointing one way.
    """

    @property
    def model(self) -> str: ...

    @property
    def endpoint(self) -> str: ...

    @property
    def api_key(self) -> str | None: ...

    @property
    def options(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class WireRequest:
    """One request, described but not performed.

    Rendering and sending are separate so the shape can be asserted without a
    network, and so a caller keeps whatever HTTP client it already has — the
    same reason `ModelClient` takes a transport rather than owning one.
    """

    url: str
    payload: Mapping[str, Any]
    headers: Mapping[str, str] = field(default_factory=dict)
    method: str = "POST"


class RequestAdapter(Protocol):
    """Turns a role's messages into a request on the wire."""

    @property
    def name(self) -> str: ...

    def render(
        self,
        route: RouteLike,
        messages: Sequence[Mapping[str, Any]],
        options: Mapping[str, Any],
    ) -> WireRequest: ...


class AuthStrategy(Protocol):
    """Attaches a credential, or deliberately does not."""

    @property
    def name(self) -> str: ...

    def headers(self, route: RouteLike, api_key: str | None) -> Mapping[str, str]: ...


class ResponseReader(Protocol):
    """Reads the two things the harness needs out of a successful body."""

    @property
    def name(self) -> str: ...

    def text(self, body: bytes | str | None) -> str | None: ...

    def usage(self, body: bytes | str | None) -> Mapping[str, int] | None: ...


def join_url(endpoint: str, path: str) -> str:
    """An endpoint and a configured path, without doubling or dropping a `/`."""
    if not path:
        return endpoint
    return f"{endpoint.rstrip('/')}/{path.lstrip('/')}"


@dataclass(frozen=True)
class JsonChatRequest:
    """A JSON POST carrying a model name and a list of messages.

    Every part of that sentence is a configured value: the path appended to the
    endpoint, the key the model goes under, the key the messages go under, and
    the option keys that instruct the transport rather than the model. The
    default appends no path at all, because a base URL that already addresses
    the completion endpoint is the only assumption available to a preset that
    claims nothing.

    `transport_options` exists because `timeout` and `role` are instructions to
    this process. Sending them as completion parameters is a request most
    providers reject, and a probe that inherited a work timeout would take ten
    minutes to establish that a model is not answering.
    """

    name: str = "json-chat"
    path: str = ""
    model_key: str = "model"
    messages_key: str = "messages"
    transport_options: tuple[str, ...] = ("role", "timeout")
    extra_payload: Mapping[str, Any] = field(default_factory=dict)

    def render(
        self,
        route: RouteLike,
        messages: Sequence[Mapping[str, Any]],
        options: Mapping[str, Any],
    ) -> WireRequest:
        payload: dict[str, Any] = {
            self.model_key: route.model,
            self.messages_key: list(messages),
            **self.extra_payload,
        }
        payload.update({k: v for k, v in options.items() if k not in self.transport_options})
        return WireRequest(url=join_url(route.endpoint, self.path), payload=payload)


@dataclass(frozen=True)
class BearerAuth:
    """A credential in one header, scheme optional.

    One class rather than several, because ``Authorization: Bearer …`` and
    ``x-api-key: …`` differ only in a header name and whether a scheme word
    precedes the value. A preset that needs the second is a construction, not a
    subclass.

    An absent credential sends no header instead of an empty one. A local
    server that wants no authentication is a supported configuration, and
    ``Authorization: Bearer`` with nothing after it is a malformed request
    rather than an anonymous one.
    """

    name: str = "bearer"
    header: str = "Authorization"
    scheme: str = "Bearer"

    def headers(self, route: RouteLike, api_key: str | None) -> Mapping[str, str]:
        key = route.api_key or api_key
        if not key:
            return {}
        return {self.header: f"{self.scheme} {key}" if self.scheme else key}


@dataclass(frozen=True)
class NoAuth:
    """Sends no credential, and says so rather than leaving it to chance."""

    name: str = "none"

    def headers(self, route: RouteLike, api_key: str | None) -> Mapping[str, str]:
        return {}


def _payload_of(body: bytes | str | None) -> Any:
    if body is None:
        return None
    try:
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        return json.loads(text)
    except (ValueError, AttributeError):
        return None


def _walk(payload: Any, path: str) -> Any:
    """A dotted path through parsed JSON, with numeric list indices.

    Returns None at the first step that is not there. A reader that raised on a
    changed response shape would turn a vendor's field rename into a failed
    call rather than an unread one.
    """
    current = payload
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                index = int(part)
            except ValueError:
                return None
            if not -len(current) <= index < len(current):
                return None
            current = current[index]
        else:
            return None
        if current is None:
            return None
    return current


@dataclass(frozen=True)
class JsonResponseReader:
    """Text and token counts from a JSON body, conservatively.

    `text_paths` are tried in order and the first non-empty string wins. The
    generic reader configures none, because there is no vendor-neutral place
    for assistant text to be, and inventing one would mean reporting an empty
    answer for a body this build simply does not know how to read.

    Usage is delegated to `pricing`, with the key names configurable. It
    returns None — not zero — for a body reporting nothing. Zero tokens is a
    measurement; a parser that emits it on an unrecognised shape produces a
    cost series that is quietly wrong and never complains.
    """

    name: str = "json"
    text_paths: tuple[str, ...] = ()
    usage_key: str = "usage"
    tokens_in_keys: tuple[str, ...] = pricing.INPUT_TOKEN_KEYS
    tokens_out_keys: tuple[str, ...] = pricing.OUTPUT_TOKEN_KEYS
    cached_token_keys: tuple[str, ...] = pricing.CACHED_TOKEN_KEYS

    def text(self, body: bytes | str | None) -> str | None:
        payload = _payload_of(body)
        if payload is None:
            return None
        for path in self.text_paths:
            value = _walk(payload, path)
            if isinstance(value, str) and value:
                return value
        return None

    def usage(self, body: bytes | str | None) -> Mapping[str, int] | None:
        return pricing.extract_usage(
            body,
            usage_key=self.usage_key,
            tokens_in_keys=self.tokens_in_keys,
            tokens_out_keys=self.tokens_out_keys,
            cached_token_keys=self.cached_token_keys,
        )


@dataclass(frozen=True)
class RoutePreset:
    """A named, complete answer to "how is this endpoint spoken to".

    A preset is the documented bundle §7.1 asks for: one name that supplies a
    request adapter, an authentication strategy, a response reader and a
    failure classifier at once, so a route can be four words of configuration
    instead of four objects.

    `hosts` is *only* consulted by `suggest()`. It is a hint printed for a
    human, never an input to resolution.
    """

    name: str
    request: RequestAdapter
    auth: AuthStrategy
    reader: ResponseReader
    classifier: Provider
    hosts: tuple[str, ...] = ()
    summary: str = ""

    def describe(self) -> str:
        """One line an operator can check a deployment against."""
        return (
            f"{self.name}: protocol={self.request.name} auth={self.auth.name} "
            f"reader={self.reader.name} classifier={self.classifier.name}"
        )


#: The default core route. It makes no vendor-specific claim and is the only
#: preset this module defines: a JSON POST to the endpoint as configured, a
#: bearer credential when one is supplied, conservative usage reading, and
#: classification from HTTP alone.
#:
#: Its reader knows no text path, which is deliberate. A generic build hands
#: the raw body to the caller rather than pretending to have found an answer
#: in a shape it has never seen.
GENERIC_PRESET = RoutePreset(
    name="generic",
    request=JsonChatRequest(),
    auth=BearerAuth(),
    reader=JsonResponseReader(),
    classifier=GENERIC,
    summary=(
        "Generic JSON chat over the endpoint exactly as configured. Cannot tell "
        "a spend cap from a burst limit, because nothing in HTTP can."
    ),
)

#: Presets built in this process, plus the ones already resolved by name.
#: Populated by `register()`; consulted before anything is imported.
_REGISTERED: dict[str, RoutePreset] = {}

_BUILTIN: dict[str, RoutePreset] = {GENERIC_PRESET.name: GENERIC_PRESET}


def register(preset: RoutePreset, *, replace: bool = False) -> RoutePreset:
    """Make a preset resolvable by name in this process.

    Refuses to shadow a different preset already registered under the same
    name unless asked to. Two packages quietly claiming one name would make a
    fleet's wire shape depend on import order.
    """
    if preset.name in _BUILTIN and not replace:
        raise ValueError(f"{preset.name!r} is a built-in preset; pass replace=True to shadow it")
    existing = _REGISTERED.get(preset.name)
    if existing is not None and existing != preset and not replace:
        raise ValueError(
            f"a different preset is already registered as {preset.name!r}; "
            "pass replace=True if that is intended"
        )
    _REGISTERED[preset.name] = preset
    return preset


def _configured_targets() -> dict[str, str]:
    """`name -> module:attribute`, from configuration. Nothing is imported."""
    targets: dict[str, str] = {}
    for entry in os.environ.get(PRESET_PATH_ENV, "").split(","):
        text = entry.strip()
        if not text:
            continue
        name, _, target = text.partition("=")
        if not target.strip():
            log.warning(
                "protocols: ignoring %r in $%s; expected name=module:attribute",
                text,
                PRESET_PATH_ENV,
            )
            continue
        targets[name.strip()] = target.strip()
    return targets


def _declared_targets() -> dict[str, str]:
    """`name -> module:attribute`, from installed distributions.

    Reads the entry-point *metadata*. No declaring module is imported here,
    which is what makes an adapter cost nothing until a route names it.
    """
    from importlib.metadata import entry_points

    try:
        return {point.name: point.value for point in entry_points(group=ENTRY_POINT_GROUP)}
    except Exception:  # noqa: BLE001 - broken metadata must not stop routing
        log.warning("protocols: could not read %s entry points", ENTRY_POINT_GROUP, exc_info=True)
        return {}


def _load_target(name: str, target: str) -> RoutePreset:
    module_name, _, attribute = target.partition(":")
    module = importlib.import_module(module_name)
    found: Any = getattr(module, attribute) if attribute else module
    if callable(found) and not isinstance(found, RoutePreset):
        found = found()
    if not isinstance(found, RoutePreset):
        raise TypeError(f"{target!r} is not a RoutePreset (declared as {name!r})")
    if found.name != name:
        # The declared name is the one routes use, so it wins; say so rather
        # than leaving an operator to wonder why their name does not resolve.
        log.info("protocols: %r declares preset %r; resolving it as %r", target, found.name, name)
    return found


def find(name: str) -> RoutePreset | None:
    """The preset called `name`, or None.

    Loads at most the one module that declares that name. A build with twenty
    adapters installed imports the one a route asked for.
    """
    if not name:
        return GENERIC_PRESET
    for source in (_BUILTIN, _REGISTERED):
        if name in source:
            return source[name]
    for targets in (_configured_targets(), _declared_targets()):
        target = targets.get(name)
        if target is None:
            continue
        try:
            preset = _load_target(name, target)
        except Exception:  # noqa: BLE001 - a broken plugin is a named failure
            log.warning("protocols: could not load preset %r from %r", name, target, exc_info=True)
            return None
        # Remembered, so a role called on every item does not re-import.
        _REGISTERED[name] = preset
        return preset
    return None


def resolve(name: str) -> RoutePreset:
    """The preset called `name`, or `UnknownPreset` naming the ones that exist."""
    preset = find(name)
    if preset is None:
        raise UnknownPreset(name, names())
    return preset


def names() -> list[str]:
    """Every preset name that can be resolved, without loading any of them."""
    return sorted({*_BUILTIN, *_REGISTERED, *_configured_targets(), *_declared_targets()})


def load_all() -> dict[str, RoutePreset]:
    """Every declared preset, loaded. Used for reporting, never for routing.

    Resolution deliberately loads one module; describing what is available
    loads all of them, and that difference is the whole reason `find()` takes
    a name.
    """
    loaded: dict[str, RoutePreset] = {}
    for name in names():
        preset = find(name)
        if preset is not None:
            loaded[name] = preset
    return loaded


@dataclass(frozen=True)
class Suggestion:
    """A guess, offered to a human, acted on by nobody.

    `why` is written to be printed. An operator who reads it can set the
    preset; nothing in the harness will set it for them.
    """

    preset: str
    host: str
    why: str


def suggest(endpoint: str) -> Suggestion | None:
    """Which preset an endpoint's host looks like it wants.

    Reported only. Detection from a hostname is a good hint and a terrible
    decision procedure: hosts are proxied, renamed, self-hosted and shared, and
    a protocol nobody configured is one nobody can audit. Routing never calls
    this — `tests/test_route_presets.py` asserts that.
    """
    host = (urlsplit(endpoint).hostname or "").lower()
    if not host:
        return None
    for name, preset in sorted(load_all().items()):
        for candidate in preset.hosts:
            hint = candidate.lower().lstrip(".")
            if host == hint or host.endswith(f".{hint}"):
                return Suggestion(
                    preset=name,
                    host=host,
                    why=(
                        f"{host} matches the {name!r} preset ({preset.describe()}). "
                        "Nothing has been chosen: set the route's preset to use it."
                    ),
                )
    return None


#: What an entry point or a `$HARNESS_ROUTE_PRESETS` target may be, besides a
#: `RoutePreset` itself: a zero-argument callable that builds one, for a plugin
#: whose preset depends on something it has to read first.
PresetFactory = Callable[[], RoutePreset]
