# Next steps

**Current target:** [`minimal.md`](minimal.md)

**Current state and milestone exits:** [`docs/STATUS.md`](docs/STATUS.md)

**Exploration-complete implementation backlog:** [`BACKLOG.md`](BACKLOG.md)

This file previously contained a Rainmon/Node-B acceptance runbook. That
runbook mixed one consumer's repository, credentials, CI policy, deployment
topology, and prerequisites with the generic harness product. It is superseded.

Rainmon may be used later as one real acceptance project, but its requirements
must be supplied by a valid project plan and execution profile. They are not
changes that the harness core should require every user to make.

The current implementation order is:

1. implement the versioned generic minimum plan and one-pass validator;
2. make valid-plan admission reviewed, atomic, and idempotent;
3. persist and preflight a per-project execution profile, including an existing
   immutable runner image or a reviewable locally generated image;
4. complete the admitted plan through the existing local worktree, gate,
   review, commit, and integration-branch machinery against a real daemon;
5. add the integrated build, local deploy, readiness, acceptance, teardown, and
   recovery lifecycle for the product under development;
6. prove the same core with two materially different projects and local
   topologies.

Do not add hosted CI/CD, remote branch/issue/review mutation, or a non-local
deployment prerequisite to unblock these milestones. Do not add a project name,
repository layout, language, toolchain, or topology to core. If a local topology
needs specific knowledge, supply it through configuration or an installed
adapter.

This file intentionally does not duplicate detailed tasks or status. The
normative boundary lives in `minimal.md`, the implementation-ready work and
dependency spine live in `BACKLOG.md`, and the current milestone/evidence
record lives in `docs/STATUS.md`.
