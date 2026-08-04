# Proposal — make the coordination plane worth having

**Status:** proposal, not accepted. Written 2026-08-04, after connecting the
oversight actor to a real fleet and running it against a real model.

It builds on [`COORDINATION-PLANE.md`](COORDINATION-PLANE.md) and settles none
of its open decisions. Where the two disagree about the *design*, that document
wins; this one proposes what to build next and in what order, and says what
each step is worth.

---

## 1. What exists now, exactly

Landed on `feat/oversight-actor`:

| Piece | State |
|---|---|
| Message ledger, rooms, idempotency (§4) | Built |
| Command service, typed proposals, journal (§7) | Built |
| Oversight actor, lease, fencing (§6) | Built |
| **Workers writing to the ledger** | Built — new, and the reason any of the above is reachable |
| `oversight_bridge.py` — the actor over a real queue | Built |
| Rooms *per item* (§5) | **Not built.** Everything goes to the general room |
| Agents *reading* the room | **Not built.** Nothing flows back |
| Agent-to-agent messages | **Not built,** and not currently possible |
| Human participation over the API (§9) | **Not built** |

The honest summary: a worker can now say something, and a coordinator can hear
it and act. Nothing else in the conversation exists. It is a one-way radio.

## 2. The measurement this proposal comes from

A real model, in the oversight role, over three genuine repeated failures:

> The same formatting check failed three times on item R2, but **I was not
> given the diff, file paths, or any evidence of what needs changing**.
> Proposing a concrete fix without that evidence would be guesswork.

It escalated to a human rather than routing around the problem. That is
correct behaviour and a poor outcome, and the fault is entirely in what it was
given. A worker's message today is:

```
checks_failed: `cargo fmt --all -- --check` failed:
```

— a stage name and a truncated tail. The harness *has* the check's full
output, the diff it just tried to apply, the file paths the planner named, and
the patch artefact it wrote to disk. None of it is in the room.

This matters beyond politeness. During the `rdpapp` import, a human doing the
coordinator's job resolved four situations by hand — swapping a gate that
refused five correct changes, spotting an item whose title contradicted its own
scope note, dropping a dependency that was sequencing rather than necessity,
and deciding when to stop paying for an item. **Every one of those decisions
required evidence that is not currently in any room.**

## 3. What to build, in order

Each step is independently shippable and independently useful. Each has a
reason to exist that is not "the design says so".

### 3.1 Evidence in the message — *the one that unblocks everything else*

Give an observation the artefacts a person would open. Concretely, on the
failing stages the executor already reports:

| Stage | Attach |
|---|---|
| `checks_failed` | the command, exit code, and the tail of its output the item was refused for |
| `apply_failed` | the path to the kept patch artefact, and the rungs that were tried |
| `review_rejected` | the reviewer's verdict text in full |
| `context_unavailable` | the target, its size, and the budget |

`Submission.attachments` already exists and is unused. This is the cheapest
change on this list and the largest change in what a coordinator can conclude.

**Bounded, because a room is not a log.** An attachment is a *reference plus a
bounded excerpt*, never a whole build. The excerpt is the part the tool itself
chose to print about the failure, which is the part a person reads.

### 3.2 A room per item

Everything currently lands in the general room, so a coordinator polling one
project reads every item's traffic interleaved. §5 already specifies
`work:{item_id}`, the actor already takes a `room_id`, and the ledger already
supports it. This is mostly wiring, and without it the general room becomes
unreadable the moment a fleet runs more than two items.

Keep the general room for project-level traffic — nothing about *one* item.

### 3.3 The return path: a worker reads what was said to it

Today a coordinator's answer goes into the room and no worker ever reads one.
The next attempt is instead handed `last_error` by
`Executor._prior_failure_prompt` — a hard-coded stand-in I wrote precisely
because there was no channel.

Replace the stand-in with the real thing: before an attempt, read the item's
room and put what is addressed to this worker into the prompt. Then a
coordinator's guidance reaches the model doing the work, which is the entire
point of having a coordinator.

**This is where the design earns out or does not.** Everything before it makes
observation better; this is the first step that closes a loop.

### 3.4 Workers can ask, and wait

`message_type: "question"` exists in the ledger and no worker sends one.
`holds.py` independently implements "an item waiting on a person" with a
durable lease and a resume token. They are two halves of one feature that have
never been introduced.

A worker that hits a genuine ambiguity — the case `AGENTS.md` says must reach a
human rather than be guessed — should ask *in the room* and hold. The hold
already survives worker death; the question already survives everything.

### 3.5 Human participation over the API

Rooms are useless to a person until they can be read and posted to. §9's
publication boundary and the read/long-poll routes from §5, with the rendering
left to the session host as the existing rule requires.

### 3.6 Agent-to-agent, last and only if wanted

Two workers talking directly is the headline feature of the original design and
the least evidenced by anything measured so far. Every problem in §2 is
worker↔coordinator or worker↔human. Direct agent-to-agent traffic should wait
until something demonstrates a need for it, rather than being built because it
is the interesting part.

## 4. What this proposal does not do

- **It does not touch a gate.** No step lets a coordinator approve work,
  weaken a check or overrule a reviewer. The actions available stay reversible
  and human-undoable.
- **It does not re-open D10 or D11.** The change protocol and attempt
  resumption are untouched; §3.3 adds *guidance* to a new attempt, which is not
  resumption and must not become it.
- **It does not make oversight required.** Absence stays safe at every step.
  A deployment that runs no coordinator sees §3.1 as slightly richer events
  and nothing else.

## 5. Open questions

**Q1 — does guidance from a coordinator make an attempt *its* attempt?**
If a coordinator's advice reaches the implementer and the work then fails,
whose failure is it, and does it consume an attempt? I do not think this can
be answered before §3.3 exists to be measured, but it must be answered before
it is switched on by default.

**Q2 — what stops a room becoming a bill?** Every message a coordinator reads
is tokens. §3.1 makes messages bigger and §3.2 makes them more numerous. There
is currently no ceiling on what one cycle may read, and there should be one
before this runs unattended.

**Q3 — should a coordinator see other projects?** The actor is per project by
construction, and a fleet-wide pattern — one gateway failing everything — is
invisible from inside one. Deliberately not proposed here; it is a different
actor with a different blast radius.

## 6. Cost, honestly

§3.1 and §3.2 are a day's work between them and carry almost no risk. §3.3 is
the one with a real design question attached (Q1) and is where I would expect
review to be slowest. §3.4 and §3.5 are ordinary feature work. §3.6 should not
be started.

The thing worth saying plainly: **none of this makes the harness better at
writing code.** It makes it better at noticing that it is not, and at telling
someone. On the evidence of the `rdpapp` import — where a human intervened
four times in six items — that is the constraint worth spending on.
