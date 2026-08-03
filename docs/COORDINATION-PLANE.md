# Agent coordination and oversight

Status: proposed, not implemented. Written 2026-08-03.

This document proposes a first-class coordination plane through which agents
can speak to one another, an oversight agent can direct traffic, and the
harness can reconcile incomplete or inconsistent work state before agents
publish incidental traffic to GitHub.

The intended result is not merely a chat endpoint. It is a permanent,
project-scoped communication ledger combined with typed state, deterministic
safety gates, and a bounded oversight role.

## 1. Goals

The coordination plane should:

- let worker agents, oversight agents and humans exchange messages directly;
- retain every accepted message permanently;
- let an oversight agent investigate incomplete dependencies and route work;
- distinguish conversation, proposed action and authoritative state;
- keep GitHub focused on externally meaningful backlog and delivery records;
- remain generic across repositories, languages, providers and agent CLIs;
- preserve every existing execution and review gate;
- isolate projects so one coordinator or failed model route cannot stall the
  rest of the fleet.

The oversight agent is a participant in the control plane. It is not itself a
safety mechanism. Claims, dependency admission, ownership, checks and review
remain deterministic even when the oversight agent is unavailable or wrong.

## 2. Findings in the current implementation

The existing pieces do not form a communication system:

- The session prompt tells an agent to stop when a dependency is absent, but
  there is no structured path for reporting that finding or receiving a
  response. A clean tree is reported only as "no changes".
- `SessionHost` can create a session and wait for it to exit, but it has no
  structured message operation.
- Dependencies are untyped strings. A dependency that is absent from the
  local queue is currently treated as satisfied, because it might be tracked
  externally. This makes an external dependency indistinguishable from a
  misspelling or omitted work item.
- A dependency is checked when work is claimed, but not continuously at later
  execution boundaries. Updating a claimed item's graph can therefore leave
  invalid work running; this is tracked by issue
  [#107](https://github.com/TheDancingDeveloper-org/agent-harness/issues/107).
- GitHub draft pull requests act as the durable checkpoint before the
  expensive reviewer gate. That is correct about durability, but makes an
  external collaboration system carry internal execution traffic.
- The operational queue is disposable, while raw audit events may be thinned
  after rollup. Neither lifecycle can satisfy permanent message retention.

A free-form shared chat would improve visibility but would not resolve
authority, delivery, dependency truth, or safe intervention.

## 3. Proposed architecture

```text
Worker agents --+
Humans ----------+--> scoped messaging API and CLI
Other agents ----+              |
                                 v
                     Permanent Message Ledger
                     append-only; retained forever
                                 |
                     per-project oversight actor
                       leased and event-driven
                                 |
                       typed action proposals
                                 v
                   deterministic Command Service
                  preconditions, policy, idempotency
                      |          |           |
                  Work Graph  Execution  External adapters
                                           GitHub/session host
```

The design separates four kinds of information:

1. **Conversation** records what participants said.
2. **Proposals** record what a participant recommends doing.
3. **Commands** are validated requests to change authoritative state.
4. **State** remains in the component that owns it, such as the work graph,
   claims, sessions or an external-system adapter.

A message never becomes queue state merely because an agent asserted it. A
proposal never becomes an action merely because the oversight model emitted
it.

## 4. Permanent message ledger

Messages belong in a third durable store, for example
`coordination.sqlite`, separate from:

- `harness.sqlite`, which contains mutable, rebuildable operational state;
- `audit.sqlite`, whose telemetry has rollup and thinning semantics.

### 4.1 Retention invariant

Once a message has been accepted, it is never edited, deleted, compacted,
rolled up or replaced. This applies equally to messages written by agents,
humans, the oversight actor and the system itself.

Replies, acknowledgements, corrections, routing decisions, access
restrictions and action results are new append-only records. A correction
references the original; it does not rewrite it. Search indexes, cached views
and summaries may be rebuilt or discarded, but they never replace the source
messages.

The service must not acknowledge acceptance until the durable record exists.
If the ledger is unavailable, message submission fails clearly instead of
silently degrading and losing traffic.

Permanent retention requires a distinct backup policy with restoration tests.
The ledger must not share the failure domain of the disposable work queue.

### 4.2 Message envelope

Every message should carry at least:

- immutable message ID;
- monotonic sequence within its room;
- project and room IDs;
- sender identity and intended recipients;
- message type;
- related work item, attempt and session IDs when applicable;
- reply, correlation and causation IDs;
- full body and optional structured payload;
- creation time and idempotency key;
- schema version;
- optional previous-record hash for tamper evidence.

Initial message types should include:

- `observation`;
- `question` and `answer`;
- `dependency_found`;
- `action_proposal`;
- `decision`;
- `command_accepted` and `command_rejected`;
- `delivery_receipt`;
- `system_notice`.

Acknowledging or reading a message does not mutate it. Delivery and read
receipts are additional messages or append-only receipt records.

### 4.3 Content and access

Permanent retention makes secret handling important: an accidentally posted
credential cannot later be deleted without violating the record contract.
Submission should therefore support pre-acceptance secret detection, scoped
authentication and encryption at rest. If content must later be hidden from
ordinary readers, an append-only access-restriction record can limit its
visibility without rewriting the stored message.

Large attachments should use immutable content-addressed storage. The message
retains the attachment's digest, size, media type and durable location.

## 5. Rooms and agent protocol

The chat-room metaphor is useful as a presentation model:

- each project has a general room;
- each work item has a room;
- participants can address particular roles or agents within a room;
- the oversight actor subscribes to every room in its project.

Agents receive short-lived credentials scoped to their project, item and
attempt. A generic CLI could provide operations such as:

```text
agent-harness talk send --type dependency_found --message "Schema task is absent"
agent-harness talk ask --wait "Which component owns this format?"
agent-harness talk read --after CURSOR
agent-harness talk acknowledge MESSAGE_ID
```

The executor includes the protocol and identity in the initial prompt.
CLI-specific context or transcript discovery belongs in adapters, loaded
lazily; the core protocol must not know a particular agent product's files or
conventions.

The API should support cursor-based reads and long polling initially. A later
streaming transport can improve latency without changing the message model.
All routes require explicit response models and documented fields.

The rendered room belongs in the session host, such as AIDevEnv. This
repository continues to serve JSON and does not add a second GUI.

### 5.1 Do not inject arbitrary text into terminals

The oversight actor should not type responses directly into a live PTY. The
process might currently be at a shell, an approval prompt or inside another
program; text intended as an answer can become an executable command.

Agents should retrieve messages through the structured protocol, or the
executor should deliver them at an explicit safe checkpoint. A future
session-host capability may expose a typed agent-message channel, but it
should remain distinct from raw terminal input.

## 6. Oversight actor

There should be one active oversight actor per project. It is selected by a
lease or compare-and-set operation so a restart cannot create two actors that
both believe they are authoritative. There must be no mutable process-global
coordinator shared across projects or API instances.

The actor is triggered by durable facts and messages, including:

- an agent reporting a missing or conflicting dependency;
- unresolved dependency references at admission;
- a dependency change invalidating a live claim;
- waiting-for-input or lack-of-progress signals;
- a claim without a matching live worker/session;
- failure, exhaustion or a rejected action proposal;
- plan or external-state reconciliation.

It may read conversation, work state, plans, session state and read-only
adapter data. It emits a structured response rather than relying on prose to
cause action.

### 6.1 Allowed automatic actions

Subject to the command service's preconditions, oversight may:

- route and answer messages;
- request targeted information from an agent or human;
- block unresolved work at the next safe boundary;
- reconcile a projection when authoritative state proves it stale;
- append explanations and evidence for every decision;
- notify affected participants.

### 6.2 Actions normally requiring approval

Oversight should propose, rather than unilaterally perform:

- dependency edits based on inference rather than authoritative evidence;
- creation or deletion of project scope;
- project configuration changes;
- terminating or releasing a live session;
- bypassing normal publication timing;
- writes to an external system such as GitHub.

The exact approval policy can be configured per project, but weakening a gate
is never an available policy.

### 6.3 Prohibited actions

The oversight actor must not:

- mark a dependency complete without authoritative evidence;
- override checks or reviewer verdicts;
- retry terminal cost caps;
- steal or fabricate claim ownership;
- mutate queue storage directly;
- edit or delete messages;
- pause unrelated projects because its own route is unavailable.

If the oversight model is unavailable, unresolved work remains safely
blocked, while unrelated work whose deterministic gates are satisfied can
continue.

## 7. Command service and authority

All state changes pass through one deterministic command service, regardless
of whether the caller is a human, worker, oversight actor or adapter. The
oversight model never receives database or GitHub credentials.

An action proposal contains:

- action type and target;
- expected graph revision;
- expected item state, owner and attempt;
- reason and evidence-message IDs;
- idempotency key;
- risk classification and approval requirement;
- expiry time.

The service validates current state, ownership, dependency and gate policy in
one transaction where possible. Stale proposals are rejected and the result
is appended to the originating room. Applying the same accepted command more
than once has the same effect as applying it once.

This is necessary for crash recovery: if the process dies after changing
state but before recording the result, replay must discover the already
applied idempotency key rather than performing the action twice.

## 8. Typed work graph

Replace `depends_on: list[str]` with explicit dependency edges. A reference
should identify:

- source work item;
- target kind and target identity;
- resolver or adapter when external;
- required versus advisory relationship;
- resolution state such as `unresolved`, `blocked` or `satisfied`;
- provenance and evidence;
- graph revision.

Target kinds can remain generic, for example local work, external reference,
human decision or cross-project work. Tool-specific formats and resolution
logic belong in adapters.

The admission rule becomes unambiguous: every required edge must be
explicitly satisfied before claim. An unresolved or missing target is not
equivalent to a satisfied external dependency.

### 8.1 Missing-dependency flow

When an item references an unavailable prerequisite:

1. Admission records a permanent `dependency_found` or
   `dependency_unresolved` message and does not claim the item.
2. Oversight searches the plan, work graph, room history and read-only
   external resolvers.
3. If authoritative data proves the graph projection stale, it submits an
   idempotent reconciliation command.
4. If the reference appears to be a typo, a legitimate external dependency
   or omitted work, it proposes the corresponding graph correction.
5. If it cannot resolve the ambiguity, it asks a targeted question and the
   item remains blocked.

### 8.2 Dependency discovered after claim

A live agent should not be killed implicitly. If a graph update invalidates
its claim, mark the attempt `dependency-invalidated`, notify its room and let
the current agent session reach a safe boundary. It must not cross the next
durable or external gate until the dependencies are satisfied or an explicit
operator override has been accepted and recorded.

This behaviour belongs to existing issue
[#107](https://github.com/TheDancingDeveloper-org/agent-harness/issues/107)
and should not be duplicated by the coordination epic.

## 9. GitHub publication boundary

GitHub should remain the externally visible backlog and delivery system, not
the live conversation bus. Worker agents should not receive GitHub write
credentials. Read-only context can be provided through an adapter; writes go
through the command and publication services.

Preserve the binding rule to checkpoint before the expensive gate by
introducing a `CheckpointStore` abstraction. A Git-backed implementation can
retain a commit or bundle on durable internal storage after cheap gates pass.
It need not immediately create a remote branch or draft pull request.

After review and coordination approval, the GitHub publisher can:

- push the candidate branch;
- open or update its pull request;
- record the reviewer result;
- synchronize externally meaningful backlog state.

This keeps rejected attempts and tentative coordination out of GitHub without
sacrificing crash durability. If a deployment intentionally continues using
draft PRs as its checkpoint store, that remains an adapter policy rather than
a core assumption.

## 10. Relationship to existing issues

The proposal should be implemented without duplicating these issues:

- [#107](https://github.com/TheDancingDeveloper-org/agent-harness/issues/107)
  owns dependency invalidation after claim.
- [#103](https://github.com/TheDancingDeveloper-org/agent-harness/issues/103)
  owns the progress evidence a coordinator may consume.
- [#104](https://github.com/TheDancingDeveloper-org/agent-harness/issues/104)
  owns deterministic claim/session reconciliation. An LLM must not decide
  lease truth.
- [#106](https://github.com/TheDancingDeveloper-org/agent-harness/issues/106)
  reinforces per-app and per-project state rather than module globals.
- [#109](https://github.com/TheDancingDeveloper-org/agent-harness/issues/109)
  is a prerequisite for correctly scoped messages and actions.
- [#110](https://github.com/TheDancingDeveloper-org/agent-harness/issues/110)
  must be resolved so the oversight role uses the same effective per-project
  routing as execution.
- [#105](https://github.com/TheDancingDeveloper-org/agent-harness/issues/105)
  is adjacent session-host UI work, not a replacement for the message API.
- [#111](https://github.com/TheDancingDeveloper-org/agent-harness/issues/111)
  establishes the typed-schema requirement every new route must follow.

Relevant closed issues establish useful boundaries:

- #45 chose SQLite leases for live claims;
- #53 separated the GitHub backlog from the atomic live claim mechanism;
- #80 and #98 made readiness an explicit gate;
- #89 added a supported, reasoned block action rather than direct database
  mutation;
- #54 established the durability purpose of the pre-review checkpoint.

The recurring theme is that inferred state should become an explicit,
inspectable contract. The coordination plane should follow that pattern.

## 11. Suggested implementation programme

Create one coordination epic with these non-overlapping work streams:

1. Permanent project-scoped message ledger and backup/restore contract.
2. Typed messaging API, scoped identity and agent CLI protocol.
3. Typed dependency graph and generic resolver-adapter contract.
4. Deterministic command service with preconditions and idempotency.
5. Per-project oversight actor with bounded authority and lease recovery.
6. Internal checkpoint abstraction and GitHub publication boundary.
7. AIDevEnv project/work-room interface.
8. Admission rule for missing or unresolved dependency references.

The epic should depend on #106, #109 and #110, reuse #107, and consume the
signals delivered by #103 and #104.

## 12. Acceptance properties

The coordination work is not complete until tests prove that:

- every accepted message survives process and database restart;
- no supported operation edits, deletes or thins a message;
- ledger unavailability cannot produce a false successful acknowledgement;
- two concurrent senders receive distinct ordered records;
- replaying a submission or command is idempotent;
- two projects with the same item ID cannot see or affect one another's room;
- a duplicate oversight process cannot become a second active authority;
- a missing required dependency cannot be claimed;
- a dependency invalidated after claim cannot silently cross its next gate;
- oversight failure does not weaken gates or pause unrelated projects;
- the oversight model cannot directly mutate storage or write to GitHub;
- corrections and access restrictions preserve the original message;
- the API exposes named, described schemas for every route and field;
- the session host can render rooms without this repository adding a GUI.

This framework has not run against a real fleet. The coordination plane must
be described and measured accordingly rather than presented as proven before
soak and multi-project validation exist.
